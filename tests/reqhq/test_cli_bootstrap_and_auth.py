"""Offline CLI tests & Hypothesis PBT for `reqhq doctor`, `reqhq auth`, and live startup preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest
from typer.testing import CliRunner

import reqhq.cli as cli_mod
from reqhq.cli import _parse_credential_keys, app
from reqhq.llm.ollama_runtime import OllamaReadinessReport, OllamaRuntimeError
from reqhq.security import EncryptedCredentialVault, PBKDF2FernetProtector

runner = CliRunner()


def _make_ready_ollama_report(
    *,
    ready: bool = True,
    daemon_running: bool = True,
    auto_started: bool = False,
    missing_models: list[str] | None = None,
    warmed_up: bool = True,
    guidance: list[str] | None = None,
) -> OllamaReadinessReport:
    return OllamaReadinessReport(
        base_url="http://127.0.0.1:11434",
        hardware_tier=0,
        binary_path="C:/Users/dev/AppData/Local/Programs/Ollama/ollama.exe",
        daemon_running=daemon_running,
        daemon_auto_started=auto_started,
        required_models=["qwen3:4b", "phi4-mini"],
        installed_models=[] if missing_models else ["qwen3:4b", "phi4-mini:latest"],
        missing_models=list(missing_models or []),
        pulled_models=[],
        active_model="qwen3:4b",
        warmed_up=warmed_up,
        ready=ready,
        guidance=list(guidance or []),
    )


def test_doctor_command_ready_unready_and_corrupted_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reqhq doctor` reports hardware, Ollama, masked vault, Java/Alloy, and fails closed when unready."""
    sessions_dir = tmp_path / "sessions"
    vault_path = sessions_dir / "credentials.vault.enc"
    fake_jar = tmp_path / "org.alloytools.alloy.dist.jar"
    fake_jar.write_bytes(b"jar-bytes")

    monkeypatch.setenv("REQHQ_ALLOY_JAR_PATH", str(fake_jar))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("REQHQ_VAULT_PATH", raising=False)

    # Seed encrypted vault with a valid key so doctor displays masked value only.
    vault = EncryptedCredentialVault(vault_path, protector=PBKDF2FernetProtector("test-secret"))
    secret_gemini = "AIzaSyDoctorSecretKey9988"
    vault.set("GEMINI_API_KEY", secret_gemini)
    monkeypatch.setattr(
        cli_mod,
        "_resolve_vault",
        lambda sdir, vault_path=None: EncryptedCredentialVault(
            vault_path if vault_path is not None else sdir / "credentials.vault.enc",
            protector=PBKDF2FernetProtector("test-secret"),
        ),
    )

    # 1. All local components ready
    monkeypatch.setattr(
        "reqhq.llm.ensure_ollama_ready",
        lambda **kwargs: _make_ready_ollama_report(ready=True, auto_started=True, warmed_up=True),
    )
    res_ok = runner.invoke(
        app,
        ["doctor", "--sessions-dir", str(sessions_dir), "--hardware-tier", "0", "--warmup"],
    )
    assert res_ok.exit_code == 0, res_ok.output
    assert "Ollama: ready=True" in res_ok.output
    assert "AIza...9988" in res_ok.output
    assert secret_gemini not in res_ok.output

    # 2. Ollama missing models -> exits 1 with actionable guidance
    monkeypatch.setattr(
        "reqhq.llm.ensure_ollama_ready",
        lambda **kwargs: _make_ready_ollama_report(
            ready=False,
            daemon_running=True,
            missing_models=["qwen3:4b"],
            warmed_up=False,
            guidance=["ollama pull qwen3:4b"],
        ),
    )
    res_unready = runner.invoke(
        app,
        ["doctor", "--sessions-dir", str(sessions_dir), "--strict-local", "--no-auto-start"],
    )
    assert res_unready.exit_code == 1
    assert "Ollama missing models: qwen3:4b" in res_unready.output
    assert "ollama pull qwen3:4b" in res_unready.output

    # 3. Corrupted vault -> doctor fails closed with exit 1
    monkeypatch.setattr(
        "reqhq.llm.ensure_ollama_ready",
        lambda **kwargs: _make_ready_ollama_report(ready=True),
    )
    vault_path.write_text("{corrupt-vault", encoding="utf-8")
    res_corrupt = runner.invoke(app, ["doctor", "--sessions-dir", str(sessions_dir)])
    assert res_corrupt.exit_code == 1
    assert "Vault: ERROR" in res_corrupt.output


def test_auth_cli_setup_status_rotate_clear_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reqhq auth {setup,status,rotate,clear}` encrypts secrets, masks output, and fails closed on errors."""
    sessions_dir = tmp_path / "sessions"
    vault_file = tmp_path / "custom.vault.enc"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRETS", raising=False)
    monkeypatch.delenv("MSAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("MSAL_TENANT_ID", raising=False)
    monkeypatch.delenv("REQHQ_VAULT_PATH", raising=False)

    # 1. Non-interactive setup when keys missing -> exit 1
    res_nonint = runner.invoke(
        app,
        ["auth", "setup", "--vault-path", str(vault_file), "--non-interactive"],
    )
    assert res_nonint.exit_code == 1
    assert "GEMINI_API_KEY" in res_nonint.output

    # 2. Invalid --keys option -> exit 1
    res_bad_keys = runner.invoke(
        app, ["auth", "setup", "--vault-path", str(vault_file), "--keys", " , "]
    )
    assert res_bad_keys.exit_code == 1

    # 3. Interactive setup with invalid placeholder first, then valid keys
    gemini_secret = "AIzaSyRealSecretKey12345"
    groq_secret = "gsk_RealGroqSecretKey67890"
    res_setup = runner.invoke(
        app,
        [
            "auth",
            "setup",
            "--sessions-dir",
            str(sessions_dir),
            "--vault-path",
            str(vault_file),
            "--keys",
            "GEMINI_API_KEY,GROQ_API_KEY",
        ],
        input=f"your-gemini-api-key-here\n{gemini_secret}\n{groq_secret}\n",
    )
    assert res_setup.exit_code == 0, res_setup.output
    assert "AIza...2345" in res_setup.output
    assert "gsk_...7890" in res_setup.output
    assert gemini_secret not in res_setup.output
    assert groq_secret not in res_setup.output
    raw_vault = vault_file.read_bytes()
    assert gemini_secret.encode("utf-8") not in raw_vault
    assert groq_secret.encode("utf-8") not in raw_vault

    # 4. auth status shows masked values only
    res_status = runner.invoke(app, ["auth", "status", "--vault-path", str(vault_file)])
    assert res_status.exit_code == 0, res_status.output
    assert "exists=True" in res_status.output
    assert "AIza...2345" in res_status.output
    assert gemini_secret not in res_status.output

    # 5. auth rotate rejects placeholder, then accepts valid replacement
    res_rot_bad = runner.invoke(
        app,
        ["auth", "rotate", "GEMINI_API_KEY", "--vault-path", str(vault_file)],
        input="your-gemini-api-key-here\n",
    )
    assert res_rot_bad.exit_code == 1

    new_gemini = "AIzaSyRotatedKeyValue9999"
    res_rot_ok = runner.invoke(
        app,
        ["auth", "rotate", "GEMINI_API_KEY", "--vault-path", str(vault_file)],
        input=f"{new_gemini}\n",
    )
    assert res_rot_ok.exit_code == 0, res_rot_ok.output
    assert "AIza...9999" in res_rot_ok.output
    assert new_gemini not in res_rot_ok.output

    # 6. auth clear --key removes single key, reports Not found on second call, then full clear
    res_del_1 = runner.invoke(
        app, ["auth", "clear", "--key", "GROQ_API_KEY", "--vault-path", str(vault_file)]
    )
    assert res_del_1.exit_code == 0, res_del_1.output
    assert "Removed: GROQ_API_KEY" in res_del_1.output

    res_del_2 = runner.invoke(
        app, ["auth", "clear", "--key", "GROQ_API_KEY", "--vault-path", str(vault_file)]
    )
    assert res_del_2.exit_code == 0, res_del_2.output
    assert "Not found: GROQ_API_KEY" in res_del_2.output

    res_clear_all = runner.invoke(app, ["auth", "clear", "--vault-path", str(vault_file)])
    assert res_clear_all.exit_code == 0, res_clear_all.output
    assert not vault_file.exists()

    # 7. Corrupted vault error paths across setup, status, clear --key
    vault_file.write_text("{not-json", encoding="utf-8")
    assert runner.invoke(app, ["auth", "status", "--vault-path", str(vault_file)]).exit_code == 1
    assert (
        runner.invoke(
            app, ["auth", "clear", "--key", "GEMINI_API_KEY", "--vault-path", str(vault_file)]
        ).exit_code
        == 1
    )
    assert (
        runner.invoke(
            app, ["auth", "setup", "--vault-path", str(vault_file), "--non-interactive"]
        ).exit_code
        == 1
    )


def test_live_startup_preflight_flows_and_mock_strict_local_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live preflight checks Ollama, prompts on first-run interactive TTY, reloads from vault, and bypasses mock/strict-local."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    vault_file = sessions_dir / "credentials.vault.enc"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("REQHQ_VAULT_PATH", raising=False)
    monkeypatch.delenv("REQHQ_STRICT_LOCAL", raising=False)

    # 1. Missing Ollama model raises OllamaRuntimeError -> preflight exits 1
    def _fail_ollama(**kwargs: Any) -> OllamaReadinessReport:
        raise OllamaRuntimeError("Missing model qwen3:4b", guidance=["ollama pull qwen3:4b"])

    monkeypatch.setattr("reqhq.llm.ensure_ollama_ready", _fail_ollama)
    with pytest.raises(cli_mod.typer.Exit) as exc_info:
        cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=False)
    assert exc_info.value.exit_code == 1

    # 2. Ollama ready + strict_local=True -> never touches vault or prompts
    ollama_calls: list[dict[str, Any]] = []

    def _ok_ollama(**kwargs: Any) -> OllamaReadinessReport:
        ollama_calls.append(kwargs)
        return _make_ready_ollama_report(ready=True, auto_started=True, warmed_up=True)

    monkeypatch.setattr("reqhq.llm.ensure_ollama_ready", _ok_ollama)
    prompt_calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_prompt_secret", lambda k: prompt_calls.append(k) or "")

    cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=True)
    assert len(ollama_calls) == 1
    assert ollama_calls[0]["warmup"] is True
    assert prompt_calls == []

    # 3. Interactive first-run: prompts for missing keys, encrypts & saves, exports to os.environ
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: True)
    scripted = {
        "GEMINI_API_KEY": ["your-gemini-api-key-here", "AIzaSyFirstRunKey1111"],
        "GROQ_API_KEY": ["gsk_FirstRunGroqKey2222"],
    }

    def _scripted_prompt(key_name: str) -> str:
        prompt_calls.append(key_name)
        return scripted[key_name].pop(0)

    monkeypatch.setattr(cli_mod, "_prompt_secret", _scripted_prompt)
    cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=False)
    assert vault_file.is_file()
    assert os.environ.get("GEMINI_API_KEY") == "AIzaSyFirstRunKey1111"
    assert os.environ.get("GROQ_API_KEY") == "gsk_FirstRunGroqKey2222"

    # 4. Subsequent run: clear env -> loads from encrypted vault WITHOUT re-prompting!
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    prompt_calls.clear()
    cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=False)
    assert prompt_calls == []
    assert os.environ.get("GEMINI_API_KEY") == "AIzaSyFirstRunKey1111"
    assert os.environ.get("GROQ_API_KEY") == "gsk_FirstRunGroqKey2222"

    # 5. Non-interactive missing keys -> warns without blocking or prompting
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    vault_file.unlink()
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: False)
    cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=False)
    assert prompt_calls == []

    # 6. Corrupted vault during preflight -> exits 1
    vault_file.write_text("{bad-json", encoding="utf-8")
    with pytest.raises(cli_mod.typer.Exit) as exc_corrupt:
        cli_mod._run_live_startup_preflight(sessions_dir=sessions_dir, strict_local=False)
    assert exc_corrupt.value.exit_code == 1

    # 7. Real _is_interactive_tty returns bool cleanly
    monkeypatch.undo()
    assert isinstance(cli_mod._is_interactive_tty(), bool)


def test_live_cli_commands_trigger_preflight_and_mock_bypasses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live elicit/finalize/export/handoff/eval-live invoke preflight; --mock strictly bypasses it."""
    sessions_dir = tmp_path / "sessions"
    prd = tmp_path / "prd.md"
    prd.write_text("# App\n- The system shall authenticate users.\n", encoding="utf-8")

    # Initialize session (offline, never calls preflight)
    preflight_calls: list[bool] = []

    def _stop_preflight(*, sessions_dir: Path, strict_local: bool = False) -> None:
        preflight_calls.append(strict_local)
        raise cli_mod.typer.Exit(code=42)

    monkeypatch.setattr(cli_mod, "_run_live_startup_preflight", _stop_preflight)

    res_init = runner.invoke(
        app,
        [
            "init",
            "--source",
            str(prd),
            "--session-id",
            "live-check",
            "--sessions-dir",
            str(sessions_dir),
        ],
    )
    assert res_init.exit_code == 0, res_init.output
    assert preflight_calls == []

    # Mock elicit/finalize/export/handoff/eval MUST bypass preflight completely
    res_mock_elicit = runner.invoke(
        app,
        ["elicit", "live-check", "--mock", "--max-turns", "1", "--sessions-dir", str(sessions_dir)],
    )
    assert res_mock_elicit.exit_code == 0, res_mock_elicit.output
    assert preflight_calls == []

    # Live elicit, finalize, export --out, handoff, eval run --mode live MUST invoke preflight
    for argv in (
        ["elicit", "live-check", "--sessions-dir", str(sessions_dir)],
        ["finalize", "live-check", "--sessions-dir", str(sessions_dir)],
        ["export", "live-check", "--out", str(tmp_path / "exp"), "--sessions-dir", str(sessions_dir)],
        ["handoff", "live-check", "--out", str(tmp_path / "hnd"), "--sessions-dir", str(sessions_dir)],
        ["eval", "run", "--mode", "live", "--out", str(tmp_path / "ev_live"), "--layers", "1"],
    ):
        res = runner.invoke(app, argv)
        assert res.exit_code == 42, f"Expected preflight hook on {argv}, got {res.exit_code}: {res.output}"

    assert len(preflight_calls) == 5


def test_build_router_wires_cloud_providers_quota_cache_and_respects_strict_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live _build_router wires Gemini/Groq, REQHQ_GEMINI_MODEL, QuotaLedger, and PromptCache (blocked in strict-local)."""
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyLiveRouterTestKey12345")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_LiveRouterTestKey67890")
    monkeypatch.setenv("REQHQ_GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.delenv("REQHQ_STRICT_LOCAL", raising=False)

    live_router = cli_mod._build_router(sessions_dir=sessions_dir, strict_local=False, mock=False)
    assert [p.name for p in live_router.cloud_providers] == ["gemini", "groq"]
    assert live_router.cloud_providers[0].default_model == "gemini-3.8-flash"
    assert live_router.quota_ledger is not None
    assert live_router.prompt_cache is not None
    assert live_router.strict_local is False

    # Placeholders are rejected and strict_local=True attaches zero cloud providers.
    strict_router = cli_mod._build_router(sessions_dir=sessions_dir, strict_local=True, mock=False)
    assert strict_router.cloud_providers == []
    assert strict_router.strict_local is True

    monkeypatch.setenv("GEMINI_API_KEY", "your-gemini-api-key-here")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    placeholder_router = cli_mod._build_router(
        sessions_dir=sessions_dir, strict_local=False, mock=False
    )
    assert placeholder_router.cloud_providers == []


def test_cli_verify_live_flag_on_doctor_and_auth_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--verify-live` invokes verify_credential_live on doctor, auth setup, and auth status."""
    sessions_dir = tmp_path / "sessions"
    vault_file = sessions_dir / "credentials.vault.enc"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("REQHQ_VAULT_PATH", raising=False)

    vault = EncryptedCredentialVault(vault_file, protector=PBKDF2FernetProtector("verify-secret"))
    vault.set("GEMINI_API_KEY", "AIzaSyLiveProbeKey123456")
    monkeypatch.setattr(
        cli_mod,
        "_resolve_vault",
        lambda sdir, vault_path=None: EncryptedCredentialVault(
            vault_path if vault_path is not None else sdir / "credentials.vault.enc",
            protector=PBKDF2FernetProtector("verify-secret"),
        ),
    )
    monkeypatch.setattr(
        "reqhq.llm.ensure_ollama_ready",
        lambda **kwargs: _make_ready_ollama_report(ready=True, warmed_up=True),
    )

    # 1. Passing live probe across doctor, auth status, and auth setup
    probe_calls: list[str] = []

    def _pass_probe(key_name: str, _value: str) -> tuple[bool, str]:
        probe_calls.append(key_name)
        return True, "live verified (HTTP 200)"

    monkeypatch.setattr("reqhq.security.verify_credential_live", _pass_probe)

    res_doc_ok = runner.invoke(
        app, ["doctor", "--sessions-dir", str(sessions_dir), "--verify-live"]
    )
    assert res_doc_ok.exit_code == 0, res_doc_ok.output
    assert "live verified" in res_doc_ok.output or "Loaded from encrypted vault" in res_doc_ok.output

    res_stat_ok = runner.invoke(
        app, ["auth", "status", "--vault-path", str(vault_file), "--verify-live"]
    )
    assert res_stat_ok.exit_code == 0, res_stat_ok.output

    res_setup_ok = runner.invoke(
        app,
        [
            "auth",
            "setup",
            "--vault-path",
            str(vault_file),
            "--keys",
            "GEMINI_API_KEY",
            "--non-interactive",
            "--verify-live",
        ],
    )
    assert res_setup_ok.exit_code == 0, res_setup_ok.output
    assert probe_calls.count("GEMINI_API_KEY") == 3

    # 2. Rejected key during doctor --verify-live fails closed with exit 1
    monkeypatch.setattr(
        "reqhq.security.verify_credential_live",
        lambda key_name, _val: (False, "Rejected by provider (HTTP 401)"),
    )
    res_doc_fail = runner.invoke(
        app, ["doctor", "--sessions-dir", str(sessions_dir), "--verify-live"]
    )
    assert res_doc_fail.exit_code == 1
    assert "source=invalid" in res_doc_fail.output
    assert "Rejected by provider (HTTP 401)" in res_doc_fail.output


@settings(derandomize=True, max_examples=50)
@given(
    keys=st.lists(
        st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{1,20}", fullmatch=True),
        min_size=1,
        max_size=8,
    )
)
def test_pbt_parse_credential_keys_uppercases_and_deduplicates_order(keys: list[str]) -> None:
    """PBT: _parse_credential_keys normalizes case, deduplicates, and preserves first-seen order."""
    raw = ", ".join(keys)
    parsed = _parse_credential_keys(raw)
    expected: list[str] = []
    for k in keys:
        up = k.upper()
        if up not in expected:
            expected.append(up)
    assert parsed == expected
    assert len(parsed) == len(set(parsed))
