"""Local Ollama auto-bootstrap, daemon lifecycle, and readiness gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from reqhq.llm.hardware_profiler import HardwareProfiler, HardwareTier
from reqhq.llm.model_registry import get_model_for_tier


class OllamaReadinessReport(BaseModel):
    """Structured readiness report for the local Ollama runtime and tier models."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    hardware_tier: int = Field(..., ge=0, le=3)
    binary_path: str | None = None
    daemon_running: bool
    daemon_auto_started: bool = False
    required_models: list[str]
    installed_models: list[str]
    missing_models: list[str]
    pulled_models: list[str] = Field(default_factory=list)
    active_model: str
    warmed_up: bool = False
    ready: bool
    guidance: list[str] = Field(default_factory=list)


class OllamaRuntimeError(RuntimeError):
    """Raised when local Ollama bootstrap or readiness verification fails."""

    def __init__(
        self,
        message: str,
        *,
        guidance: list[str] | None = None,
        report: OllamaReadinessReport | None = None,
    ) -> None:
        self.guidance: list[str] = list(guidance) if guidance is not None else []
        self.report: OllamaReadinessReport | None = report
        full_message = message
        if self.guidance:
            steps = "\n".join(f"  - {step}" for step in self.guidance)
            full_message = f"{message}\nRecovery steps:\n{steps}"
        super().__init__(full_message)


def normalize_model_tag(name: str) -> str:
    """Normalize an Ollama model tag to canonical lowercase ``repo:tag`` form.

    Examples:
        ``"phi4-mini"`` -> ``"phi4-mini:latest"``
        ``"phi4-mini:latest"`` -> ``"phi4-mini:latest"``
        ``" Qwen3:4B "`` -> ``"qwen3:4b"``
    """
    cleaned = name.strip().lower()
    if not cleaned:
        return ""
    slash_idx = cleaned.rfind("/")
    colon_idx = cleaned.rfind(":")
    if colon_idx > slash_idx:
        repo = cleaned[:colon_idx].strip()
        tag = cleaned[colon_idx + 1 :].strip()
        if not repo:
            return ""
        return f"{repo}:{tag or 'latest'}"
    return f"{cleaned}:latest"


def _compute_missing_models(
    required_models: list[str],
    installed_models: list[str],
) -> list[str]:
    """Return required models whose normalized tags are absent from installed models."""
    installed_normalized = {normalize_model_tag(model) for model in installed_models}
    return [
        model
        for model in required_models
        if normalize_model_tag(model) not in installed_normalized
    ]


class OllamaRuntimeManager:
    """Discovers, starts, verifies, pulls, and warms up local Ollama runtimes."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        client: httpx.Client | None = None,
        startup_timeout_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
        request_timeout_seconds: float = 5.0,
        warmup_timeout_seconds: float = 30.0,
        pull_timeout_seconds: float = 600.0,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self._client = client
        self.startup_timeout_seconds = startup_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.warmup_timeout_seconds = warmup_timeout_seconds
        self.pull_timeout_seconds = pull_timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        if self._client is not None:
            return self._client.request(method, url, json=json_body, timeout=timeout)
        with httpx.Client(timeout=timeout) as client:
            return client.request(method, url, json=json_body)

    def _fetch_tags(self) -> list[str] | None:
        try:
            resp = self._request("GET", "/api/tags", timeout=self.request_timeout_seconds)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        raw_models = payload.get("models", [])
        if not isinstance(raw_models, list):
            return []
        models: list[str] = []
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if name:
                models.append(name)
        return models

    def find_ollama_binary(self) -> Path | None:
        """Locate the ``ollama`` executable via PATH or Windows LocalAppData fallback."""
        discovered = shutil.which("ollama")
        if discovered:
            return Path(discovered)
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidate = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.is_file():
                return candidate
        return None

    def is_daemon_running(self) -> bool:
        """Return True when ``GET {base_url}/api/tags`` succeeds with a valid JSON object."""
        return self._fetch_tags() is not None

    def list_installed_models(self) -> list[str]:
        """List installed Ollama model names via ``GET {base_url}/api/tags``."""
        tags = self._fetch_tags()
        return tags if tags is not None else []

    def start_daemon(self, binary_path: Path | None = None) -> bool:
        """Launch ``ollama serve`` detached and poll until the daemon responds or times out."""
        binary = binary_path or self.find_ollama_binary()
        if binary is None:
            return False

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            popen_kwargs["creationflags"] = detached | no_window
        else:
            popen_kwargs["start_new_session"] = True

        try:
            subprocess.Popen([str(binary), "serve"], **popen_kwargs)
        except OSError:
            return False

        deadline = time.monotonic() + max(0.0, self.startup_timeout_seconds)
        while True:
            if self.is_daemon_running():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            time.sleep(min(max(0.0, self.poll_interval_seconds), remaining))

    def pull_model(self, model_name: str, binary_path: Path | None = None) -> bool:
        """Pull an Ollama model via ``ollama pull <model_name>``."""
        binary = binary_path or self.find_ollama_binary()
        if binary is None:
            return False
        try:
            completed = subprocess.run(
                [str(binary), "pull", model_name],
                capture_output=True,
                text=True,
                timeout=self.pull_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def warmup_model(self, model_name: str, keep_alive: str = "10m") -> bool:
        """Warm up a single active model into memory via ``POST /api/generate``."""
        payload: dict[str, Any] = {
            "model": model_name,
            "prompt": "",
            "stream": False,
            "keep_alive": keep_alive,
        }
        try:
            resp = self._request(
                "POST",
                "/api/generate",
                timeout=self.warmup_timeout_seconds,
                json_body=payload,
            )
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300

    def check_and_bootstrap(
        self,
        *,
        tier: HardwareTier | int | None = None,
        auto_start: bool = True,
        auto_pull: bool = False,
        warmup: bool = False,
        require_fallback_model: bool = True,
    ) -> OllamaReadinessReport:
        """Verify local Ollama readiness, optionally starting the daemon, pulling, and warming up."""
        if tier is None:
            resolved_tier = HardwareProfiler.detect().tier
        else:
            resolved_tier = get_model_for_tier(tier).tier

        spec = get_model_for_tier(resolved_tier)
        required_models = [spec.model_name]
        if require_fallback_model and spec.fallback_model:
            required_models.append(spec.fallback_model)

        binary = self.find_ollama_binary()
        binary_path_str = str(binary) if binary is not None else None

        daemon_running = self.is_daemon_running()
        daemon_auto_started = False
        if not daemon_running and auto_start and binary is not None:
            daemon_auto_started = self.start_daemon(binary_path=binary)
            daemon_running = daemon_auto_started

        installed_models: list[str] = []
        pulled_models: list[str] = []
        guidance: list[str] = []

        if daemon_running:
            installed_models = self.list_installed_models()
            missing_models = _compute_missing_models(required_models, installed_models)

            if missing_models and auto_pull and binary is not None:
                for model_name in list(missing_models):
                    if self.pull_model(model_name, binary_path=binary):
                        pulled_models.append(model_name)
                installed_models = self.list_installed_models()
                missing_models = _compute_missing_models(required_models, installed_models)
        else:
            missing_models = list(required_models)
            if binary is None:
                guidance.append("winget install Ollama.Ollama")
                guidance.append("ollama serve")
            else:
                guidance.append(
                    'ollama serve  # or PowerShell: Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden'
                )

        for missing in missing_models:
            guidance.append(f"ollama pull {missing}")

        ready = daemon_running and len(missing_models) == 0
        warmed_up = False
        if ready and warmup:
            warmed_up = self.warmup_model(spec.model_name)

        return OllamaReadinessReport(
            base_url=self.base_url,
            hardware_tier=int(resolved_tier),
            binary_path=binary_path_str,
            daemon_running=daemon_running,
            daemon_auto_started=daemon_auto_started,
            required_models=required_models,
            installed_models=installed_models,
            missing_models=missing_models,
            pulled_models=pulled_models,
            active_model=spec.model_name,
            warmed_up=warmed_up,
            ready=ready,
            guidance=guidance,
        )


def ensure_ollama_ready(
    *,
    base_url: str = "http://127.0.0.1:11434",
    tier: HardwareTier | int | None = None,
    auto_start: bool = True,
    auto_pull: bool = False,
    warmup: bool = False,
    require_fallback_model: bool = True,
    raise_on_unready: bool = True,
    manager: OllamaRuntimeManager | None = None,
) -> OllamaReadinessReport:
    """Run local Ollama bootstrap/readiness checks and optionally raise on failure."""
    runtime = manager if manager is not None else OllamaRuntimeManager(base_url=base_url)
    report = runtime.check_and_bootstrap(
        tier=tier,
        auto_start=auto_start,
        auto_pull=auto_pull,
        warmup=warmup,
        require_fallback_model=require_fallback_model,
    )
    if raise_on_unready and not report.ready:
        if not report.daemon_running:
            message = f"Local Ollama daemon is not reachable at {report.base_url}."
        else:
            missing_csv = ", ".join(report.missing_models)
            message = (
                f"Missing required Ollama model(s) for HardwareTier {report.hardware_tier}: "
                f"{missing_csv}."
            )
        raise OllamaRuntimeError(message, guidance=report.guidance, report=report)
    return report


__all__ = [
    "OllamaReadinessReport",
    "OllamaRuntimeError",
    "OllamaRuntimeManager",
    "ensure_ollama_ready",
    "normalize_model_tag",
]
