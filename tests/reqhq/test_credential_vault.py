"""Unit and deterministic Hypothesis property tests for reqhq.security and config vault fields."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from reqhq.config import ReqHQConfig
from reqhq.security import (
    CREDENTIAL_SPECS,
    CRYPTPROTECT_UI_FORBIDDEN,
    VAULT_ENVELOPE_VERSION,
    CredentialCheckReport,
    CredentialProvisioningError,
    CredentialSource,
    CredentialSpec,
    CredentialStatusItem,
    EncryptedCredentialVault,
    PBKDF2FernetProtector,
    SecretProtector,
    VaultDecryptionError,
    VaultError,
    WindowsDPAPIProtector,
    default_secret_protector,
    ensure_credentials_ready,
    is_valid_credential_value,
    mask_secret,
)
from reqhq.security import credential_vault as cv_mod


# ---------------------------------------------------------------------------
# Models & canonical credential specs
# ---------------------------------------------------------------------------


def test_credential_specs_and_strict_models(tmp_path: Path) -> None:
    expected_keys = {
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        "MSAL_CLIENT_ID",
        "MSAL_TENANT_ID",
    }
    assert set(CREDENTIAL_SPECS.keys()) == expected_keys
    assert CREDENTIAL_SPECS["GEMINI_API_KEY"].required_for_cloud is True
    assert CREDENTIAL_SPECS["GROQ_API_KEY"].required_for_cloud is True
    assert CREDENTIAL_SPECS["GOOGLE_OAUTH_CLIENT_SECRETS"].required_for_cloud is False
    assert CREDENTIAL_SPECS["MSAL_CLIENT_ID"].optional_under_strict_local is True
    assert CREDENTIAL_SPECS["MSAL_TENANT_ID"].optional_under_strict_local is True

    item = CredentialStatusItem(
        key_name="GEMINI_API_KEY",
        configured=True,
        valid=True,
        source=CredentialSource.VAULT,
        masked_value="AIza...9999",
        message="ok",
    )
    report = CredentialCheckReport(
        vault_path=tmp_path / "credentials.vault.enc",
        protector_backend="windows-dpapi",
        strict_local=False,
        items=[item],
        all_required_ready=True,
    )
    assert report.prompted_keys == []
    assert CredentialCheckReport.model_validate_json(report.model_dump_json()) == report

    with pytest.raises(ValidationError):
        CredentialSpec(key_name="K", description="D", extra_bad=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CredentialStatusItem(
            key_name="K",
            configured=False,
            valid=False,
            source=CredentialSource.MISSING,
            extra_bad=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        CredentialCheckReport(
            vault_path=tmp_path / "v.enc",
            protector_backend="pbkdf2-fernet",
            strict_local=False,
            items=[],
            all_required_ready=True,
            extra_bad=True,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Secret masking & credential validation
# ---------------------------------------------------------------------------


def test_mask_secret_rules() -> None:
    assert mask_secret("") == ""
    assert mask_secret("a") == "****"
    assert mask_secret("12345678") == "****"
    assert mask_secret("****") == "<redacted>"
    assert mask_secret("AIzaSyD1234567890") == "AIza...7890"
    assert mask_secret("abcd...efgh") == "abcd***efgh"


def test_is_valid_credential_value_rules() -> None:
    assert is_valid_credential_value("GEMINI_API_KEY", "AIzaSyD1234567890")[0] is True
    assert is_valid_credential_value("GROQ_API_KEY", "gsk_1234567890abcdef")[0] is True
    assert is_valid_credential_value("MSAL_TENANT_ID", "common")[0] is True
    assert is_valid_credential_value("CUSTOM_KEY", "val")[0] is True

    # Empty / whitespace
    assert is_valid_credential_value("GEMINI_API_KEY", "")[0] is False
    assert is_valid_credential_value("GEMINI_API_KEY", "     ")[0] is False

    # Control characters & newlines
    for bad in ("AIzaSyD12345\n", "AIzaSyD12345\r", "AIza\tSyD12345", "AIza\x0012345", "AIza\x7f12345"):
        ok, msg = is_valid_credential_value("GEMINI_API_KEY", bad)
        assert ok is False
        assert "control" in msg.lower()

    # Known placeholders (case-insensitive)
    for placeholder in (
        "your-gemini-api-key-here",
        "YOUR-GROQ-API-KEY-HERE",
        "00000000-0000-0000-0000-000000000000",
        "path/to/credentials.json",
        "changeme",
        "Replace-Me",
        "XXX",
    ):
        ok, msg = is_valid_credential_value("GEMINI_API_KEY", placeholder)
        assert ok is False
        assert "placeholder" in msg.lower()

    # Too-short cloud API keys
    ok_short_gem, msg_gem = is_valid_credential_value("GEMINI_API_KEY", "1234567")
    assert ok_short_gem is False
    assert "8" in msg_gem
    ok_short_groq, _ = is_valid_credential_value("GROQ_API_KEY", "short")
    assert ok_short_groq is False


# ---------------------------------------------------------------------------
# Protectors: live Windows 11 DPAPI + mocked errors + PBKDF2FernetProtector
# ---------------------------------------------------------------------------


def test_windows_dpapi_protector_live_roundtrip_and_tamper() -> None:
    assert sys.platform == "win32"
    protector = WindowsDPAPIProtector()
    assert isinstance(protector, SecretProtector)
    assert protector.backend_name == "windows-dpapi"

    payload = b"reqhq-dpapi-master-key-test-payload-0123456789"
    blob = protector.protect(payload)
    assert blob != payload
    assert payload not in blob
    assert protector.unprotect(blob) == payload

    # Empty plaintext round-trip works; empty ciphertext rejected fail-closed
    empty_blob = protector.protect(b"")
    assert protector.unprotect(empty_blob) == b""
    with pytest.raises(VaultDecryptionError, match="Empty DPAPI"):
        protector.unprotect(b"")

    # 1-byte tamper in DPAPI blob MUST raise VaultDecryptionError
    tampered = bytearray(blob)
    tampered[len(tampered) // 2] ^= 0xFF
    with pytest.raises(VaultDecryptionError, match="CryptUnprotectData"):
        protector.unprotect(bytes(tampered))


def test_windows_dpapi_protector_non_windows_and_mocked_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cv_mod.sys, "platform", "linux")
    with pytest.raises(VaultError, match="only supported on Windows"):
        WindowsDPAPIProtector()

    class _FailingCrypt32:
        def CryptProtectData(self, *_args: Any) -> int:
            return 0

        def CryptUnprotectData(self, *_args: Any) -> int:
            return 0

    class _DummyKernel32:
        def __init__(self) -> None:
            self.freed: list[Any] = []

        def LocalFree(self, ptr: Any) -> None:
            self.freed.append(ptr)

    dummy_kernel = _DummyKernel32()
    failing = WindowsDPAPIProtector(crypt32=_FailingCrypt32(), kernel32=dummy_kernel)
    with pytest.raises(VaultError, match="CryptProtectData failed"):
        failing.protect(b"secret")
    with pytest.raises(VaultDecryptionError, match="CryptUnprotectData failed"):
        failing.unprotect(b"ciphertext")

    # Verify flag constant value matches Win32 CRYPTPROTECT_UI_FORBIDDEN
    assert CRYPTPROTECT_UI_FORBIDDEN == 0x01


def test_pbkdf2_fernet_protector_roundtrip_errors_and_default_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default machine/user-scoped seed with default 600_000 iterations
    default_p = PBKDF2FernetProtector()
    assert isinstance(default_p, SecretProtector)
    assert default_p.backend_name == "pbkdf2-fernet"
    c1 = default_p.protect(b"hello-vault")
    c2 = default_p.protect(b"hello-vault")
    assert c1 != c2  # per-payload random 16-byte salt
    assert default_p.unprotect(c1) == b"hello-vault"
    assert default_p.unprotect(c2) == b"hello-vault"

    # Explicit str and bytes passphrases
    p_str = PBKDF2FernetProtector("correct-horse-battery", iterations=10_000)
    p_bytes = PBKDF2FernetProtector(b"correct-horse-battery", iterations=10_000)
    p_wrong = PBKDF2FernetProtector("wrong-passphrase", iterations=10_000)
    encrypted = p_str.protect(b"master-key-bytes")
    assert p_bytes.unprotect(encrypted) == b"master-key-bytes"

    with pytest.raises(VaultDecryptionError, match="Failed to decrypt"):
        p_wrong.unprotect(encrypted)
    with pytest.raises(VaultDecryptionError, match="truncated"):
        p_str.unprotect(b"short")

    with pytest.raises(ValueError, match="positive integer"):
        PBKDF2FernetProtector("pw", iterations=0)
    with pytest.raises(ValueError, match="must not be empty"):
        PBKDF2FernetProtector("")
    with pytest.raises(ValueError, match="must not be empty"):
        PBKDF2FernetProtector(b"")

    # default_secret_protector chooses DPAPI on win32 and PBKDF2 elsewhere
    assert isinstance(default_secret_protector(), WindowsDPAPIProtector)
    monkeypatch.setattr(cv_mod.sys, "platform", "linux")
    assert isinstance(default_secret_protector(), PBKDF2FernetProtector)


# ---------------------------------------------------------------------------
# EncryptedCredentialVault CRUD, zero plaintext, POSIX perms & fail-closed
# ---------------------------------------------------------------------------


def test_vault_crud_atomic_lf_zero_plaintext_and_posix_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_file = tmp_path / "nested" / "credentials.vault.enc"
    vault = EncryptedCredentialVault(vault_file)

    assert vault.exists() is False
    assert vault.load_all() == {}
    assert vault.get("GEMINI_API_KEY") is None
    assert vault.delete("GEMINI_API_KEY") is False
    vault.clear()  # no-op on missing file

    gem_key = "AIzaSySecretGeminiKey987654321"
    groq_key = "gsk_SecretGroqKey1234567890"
    vault.set("GEMINI_API_KEY", f"  {gem_key}  ")
    assert vault.exists() is True
    assert vault.get("GEMINI_API_KEY") == gem_key

    vault.set_many({"GROQ_API_KEY": groq_key, "MSAL_TENANT_ID": "contoso-tenant"})
    assert vault.load_all() == {
        "GEMINI_API_KEY": gem_key,
        "GROQ_API_KEY": groq_key,
        "MSAL_TENANT_ID": "contoso-tenant",
    }
    assert vault.list_masked() == {
        "GEMINI_API_KEY": "AIza...4321",
        "GROQ_API_KEY": "gsk_...7890",
        "MSAL_TENANT_ID": "cont...nant",
    }

    # Verify raw file has LF endings only, valid envelope schema, and ZERO plaintext secrets
    raw_bytes = vault_file.read_bytes()
    assert b"\r\n" not in raw_bytes
    assert gem_key.encode("utf-8") not in raw_bytes
    assert groq_key.encode("utf-8") not in raw_bytes
    assert b"contoso-tenant" not in raw_bytes
    envelope = json.loads(raw_bytes.decode("utf-8"))
    assert envelope["version"] == VAULT_ENVELOPE_VERSION
    assert envelope["backend"] == "windows-dpapi"
    assert set(envelope.keys()) == {"version", "backend", "ciphertext", "master_key_blob"}

    # Validation errors on set/set_many do not corrupt existing vault
    with pytest.raises(CredentialProvisioningError, match="empty"):
        vault.set("   ", "AIzaSyValidKey12345")
    with pytest.raises(CredentialProvisioningError, match="placeholder"):
        vault.set_many({"GEMINI_API_KEY": "AIzaSyValidKey12345", "GROQ_API_KEY": "changeme"})
    assert vault.get("GEMINI_API_KEY") == gem_key  # untouched

    # Delete existing & missing keys
    assert vault.delete("NON_EXISTENT_KEY") is False
    assert vault.delete("MSAL_TENANT_ID") is True
    assert vault.get("MSAL_TENANT_ID") is None

    # Exercise POSIX 0o600 chmod branch cleanly on Windows
    chmod_calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(cv_mod.os, "name", "posix")
    monkeypatch.setattr(cv_mod.os, "chmod", lambda p, mode: chmod_calls.append((Path(p), mode)))
    vault.set("MSAL_TENANT_ID", "posix-tenant")
    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] == (stat.S_IRUSR | stat.S_IWUSR)

    vault.clear()
    assert vault.exists() is False


def test_vault_fail_closed_on_every_corruption_and_tamper_mode(tmp_path: Path) -> None:
    protector = PBKDF2FernetProtector("unit-test-secret", iterations=1_000)
    vault_file = tmp_path / "tamper.vault.enc"
    vault = EncryptedCredentialVault(vault_file, protector=protector)
    vault.set("GEMINI_API_KEY", "AIzaSyRealKey1234567890")
    valid_envelope = json.loads(vault_file.read_text(encoding="utf-8"))

    # 1. Invalid UTF-8 bytes
    vault_file.write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(VaultDecryptionError, match="Failed to read"):
        vault.load_all()

    # 2. Malformed JSON
    vault_file.write_text("{not-json", encoding="utf-8")
    with pytest.raises(VaultDecryptionError, match="not valid JSON"):
        vault.load_all()

    # 3. Non-object JSON
    vault_file.write_text("[]", encoding="utf-8")
    with pytest.raises(VaultDecryptionError, match="JSON object"):
        vault.load_all()

    # 4. Unsupported version (including bool True == 1 edge case)
    for bad_ver in (2, True, "1"):
        bad = dict(valid_envelope, version=bad_ver)
        vault_file.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(VaultDecryptionError, match="version"):
            vault.load_all()

    # 5. Protector backend mismatch
    bad_backend = dict(valid_envelope, backend="windows-dpapi")
    vault_file.write_text(json.dumps(bad_backend), encoding="utf-8")
    with pytest.raises(VaultDecryptionError, match="backend mismatch"):
        vault.load_all()

    # 6. Missing / non-string / invalid base64 / non-canonical base64
    for bad_ct in ("", 123, "!!!not-base64!!!"):
        bad = dict(valid_envelope, ciphertext=bad_ct)
        vault_file.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(VaultDecryptionError):
            vault.load_all()

    # Non-canonical trailing padding bits (e.g. b"\x00" encodes to "AA==", while "AB==" decodes to b"\x00")
    bad_canon = dict(valid_envelope, ciphertext="AB==")
    vault_file.write_text(json.dumps(bad_canon), encoding="utf-8")
    with pytest.raises(VaultDecryptionError, match="canonical base64"):
        vault.load_all()

    # 7. Tampered ciphertext fails Fernet HMAC
    ct_bytes = bytearray(base64.b64decode(valid_envelope["ciphertext"]))
    ct_bytes[0] ^= 0x01
    bad_ct_env = dict(valid_envelope, ciphertext=base64.b64encode(bytes(ct_bytes)).decode("ascii"))
    vault_file.write_text(json.dumps(bad_ct_env), encoding="utf-8")
    with pytest.raises(VaultDecryptionError, match="integrity or decryption"):
        vault.load_all()

    # 8. Tampered master_key_blob fails protector unprotect
    mk_bytes = bytearray(base64.b64decode(valid_envelope["master_key_blob"]))
    mk_bytes[-1] ^= 0x01
    bad_mk_env = dict(
        valid_envelope, master_key_blob=base64.b64encode(bytes(mk_bytes)).decode("ascii")
    )
    vault_file.write_text(json.dumps(bad_mk_env), encoding="utf-8")
    with pytest.raises(VaultDecryptionError):
        vault.load_all()

    # 9. Protector raising arbitrary non-VaultDecryptionError wrapped cleanly
    class _BrokenProtector:
        backend_name = "pbkdf2-fernet"

        def protect(self, plaintext: bytes) -> bytes:
            return plaintext

        def unprotect(self, _ciphertext: bytes) -> bytes:
            raise RuntimeError("hardware key unavailable")

    vault_file.write_text(json.dumps(valid_envelope), encoding="utf-8")
    broken_vault = EncryptedCredentialVault(vault_file, protector=_BrokenProtector())
    with pytest.raises(VaultDecryptionError, match="Protector failed"):
        broken_vault.load_all()

    # 10. Decrypted payload is invalid JSON or non-str-dict
    def _write_crafted_payload(raw_payload: bytes) -> None:
        fkey = Fernet.generate_key()
        token = Fernet(fkey).encrypt(raw_payload)
        env = {
            "version": 1,
            "backend": protector.backend_name,
            "ciphertext": base64.b64encode(token).decode("ascii"),
            "master_key_blob": base64.b64encode(protector.protect(fkey)).decode("ascii"),
        }
        vault_file.write_text(json.dumps(env), encoding="utf-8")

    _write_crafted_payload(b"not-valid-json")
    with pytest.raises(VaultDecryptionError, match="Decrypted vault payload is not valid JSON"):
        vault.load_all()

    _write_crafted_payload(json.dumps({"GEMINI_API_KEY": 12345}).encode("utf-8"))
    with pytest.raises(VaultDecryptionError, match="map strings to strings"):
        vault.load_all()


# ---------------------------------------------------------------------------
# ensure_credentials_ready: strict_local, env, vault, interactive, retries
# ---------------------------------------------------------------------------


def test_ensure_credentials_ready_strict_local_bypass(tmp_path: Path) -> None:
    protector = PBKDF2FernetProtector("strict-local-test", iterations=1_000)
    vault = EncryptedCredentialVault(tmp_path / "credentials.vault.enc", protector=protector)

    prompted = False

    def _should_not_prompt(_key: str) -> str:
        nonlocal prompted
        prompted = True
        return "unexpected"

    report = ensure_credentials_ready(
        vault,
        strict_local=True,
        interactive=True,
        prompt_fn=_should_not_prompt,
        env={},
    )
    assert prompted is False
    assert report.strict_local is True
    assert report.all_required_ready is True
    assert report.prompted_keys == []
    assert len(report.items) == 2
    assert all("strict-local" in item.message for item in report.items)


def test_ensure_credentials_ready_env_vault_precedence_and_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protector = PBKDF2FernetProtector("precedence-test", iterations=1_000)
    vault = EncryptedCredentialVault(tmp_path / "credentials.vault.enc", protector=protector)
    vault.set_many(
        {
            "GEMINI_API_KEY": "AIzaSyVaultGeminiKey1234",
            "GROQ_API_KEY": "gsk_VaultGroqKey12345678",
        }
    )

    # Valid env wins over vault; placeholder env falls through to valid vault & exports to os.environ
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEnvGeminiKey9999")
    monkeypatch.setenv("GROQ_API_KEY", "your-groq-api-key-here")

    report = ensure_credentials_ready(vault, export_to_env=True)
    assert report.all_required_ready is True
    by_key = {item.key_name: item for item in report.items}
    assert by_key["GEMINI_API_KEY"].source == CredentialSource.ENV
    assert by_key["GEMINI_API_KEY"].masked_value == "AIza...9999"
    assert by_key["GROQ_API_KEY"].source == CredentialSource.VAULT
    assert by_key["GROQ_API_KEY"].masked_value == "gsk_...5678"
    assert os.environ["GROQ_API_KEY"] == "gsk_VaultGroqKey12345678"


def test_ensure_credentials_ready_interactive_first_run_reprompt_and_subsequent_vault_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protector = PBKDF2FernetProtector("interactive-test", iterations=1_000)
    vault = EncryptedCredentialVault(tmp_path / "credentials.vault.enc", protector=protector)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    # First-run: GEMINI gets 2 bad entries then valid on 3rd attempt; GROQ gets valid immediately
    responses = iter(
        [
            "your-gemini-api-key-here",
            "short",
            "AIzaSyPromptedGemini8888",
            "gsk_PromptedGroqKey7777",
        ]
    )
    prompted_order: list[str] = []

    def _prompt(key: str) -> str:
        prompted_order.append(key)
        return next(responses)

    report1 = ensure_credentials_ready(
        vault,
        interactive=True,
        prompt_fn=_prompt,
        export_to_env=True,
    )
    assert report1.all_required_ready is True
    assert report1.prompted_keys == ["GEMINI_API_KEY", "GROQ_API_KEY"]
    assert prompted_order == [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
    ]
    assert all(item.source == CredentialSource.PROMPTED for item in report1.items)
    assert os.environ["GEMINI_API_KEY"] == "AIzaSyPromptedGemini8888"
    assert os.environ["GROQ_API_KEY"] == "gsk_PromptedGroqKey7777"
    assert vault.exists() is True

    # Subsequent run with clean env loads directly from encrypted vault with ZERO prompts
    report2 = ensure_credentials_ready(
        vault,
        env={},
        interactive=True,
        prompt_fn=lambda _k: pytest.fail("Should not re-prompt once saved in vault"),
    )
    assert report2.all_required_ready is True
    assert report2.prompted_keys == []
    assert all(item.source == CredentialSource.VAULT for item in report2.items)


def test_ensure_credentials_ready_invalid_vault_validator_skip_cancel_and_noninteractive(
    tmp_path: Path,
) -> None:
    protector = PBKDF2FernetProtector("edge-cases-test", iterations=1_000)
    vault = EncryptedCredentialVault(tmp_path / "credentials.vault.enc", protector=protector)
    vault.set("GEMINI_API_KEY", "AIzaSyRevokedKey12345")

    # Custom validator rejects revoked key stored in vault
    def _validator(key: str, val: str) -> tuple[bool, str]:
        if "Revoked" in val:
            return False, f"{key} was revoked by provider"
        return True, "verified"

    # Non-interactive: GEMINI invalid (failed validator), GROQ missing -> fail closed
    ni_report = ensure_credentials_ready(
        vault,
        env={},
        interactive=False,
        validator_fn=_validator,
    )
    assert ni_report.all_required_ready is False
    by_key = {item.key_name: item for item in ni_report.items}
    assert by_key["GEMINI_API_KEY"].source == CredentialSource.INVALID
    assert "revoked" in by_key["GEMINI_API_KEY"].message
    assert by_key["GROQ_API_KEY"].source == CredentialSource.MISSING

    # Interactive: user cancels on GEMINI (KeyboardInterrupt) and skips GROQ ("")
    def _cancel_or_skip(key: str) -> str:
        if key == "GEMINI_API_KEY":
            raise KeyboardInterrupt
        return ""

    cancel_report = ensure_credentials_ready(
        vault,
        env={},
        interactive=True,
        prompt_fn=_cancel_or_skip,
        validator_fn=_validator,
    )
    assert cancel_report.all_required_ready is False
    cancel_by_key = {item.key_name: item for item in cancel_report.items}
    assert cancel_by_key["GEMINI_API_KEY"].source == CredentialSource.INVALID
    assert cancel_by_key["GROQ_API_KEY"].source == CredentialSource.MISSING

    # Interactive: user exhausts all 3 attempts with invalid values -> INVALID
    exhaust_report = ensure_credentials_ready(
        vault,
        required_keys=("GROQ_API_KEY",),
        env={},
        interactive=True,
        prompt_fn=lambda _k: "changeme",
    )
    assert exhaust_report.all_required_ready is False
    assert exhaust_report.items[0].source == CredentialSource.INVALID


# ---------------------------------------------------------------------------
# ReqHQConfig vault_path, auto_start_ollama & alloy fields
# ---------------------------------------------------------------------------


def test_config_vault_path_auto_start_ollama_and_alloy_env(tmp_path: Path) -> None:
    cfg_default = ReqHQConfig()
    assert cfg_default.vault_path is None
    assert cfg_default.auto_start_ollama is True
    assert cfg_default.resolved_vault_path() == Path(".reqhq_sessions/credentials.vault.enc")

    custom_vault = tmp_path / "my.vault.enc"
    cfg_explicit = ReqHQConfig(vault_path=custom_vault, auto_start_ollama=False)
    assert cfg_explicit.resolved_vault_path() == custom_vault
    assert cfg_explicit.auto_start_ollama is False

    cfg_env = ReqHQConfig.from_env(
        {
            "REQHQ_VAULT_PATH": str(custom_vault),
            "REQHQ_AUTO_START_OLLAMA": "false",
            "REQHQ_ALLOY_JAR_PATH": "C:/tools/alloy/org.alloytools.alloy.dist.jar",
            "REQHQ_ALLOY_COMPAT": "LEGACY",
        }
    )
    assert cfg_env.vault_path == custom_vault
    assert cfg_env.resolved_vault_path() == custom_vault
    assert cfg_env.auto_start_ollama is False
    assert cfg_env.alloy_jar_path == Path("C:/tools/alloy/org.alloytools.alloy.dist.jar")
    assert cfg_env.alloy_compat == "legacy"

    with pytest.raises(ValueError, match="REQHQ_AUTO_START_OLLAMA"):
        ReqHQConfig.from_env({"REQHQ_AUTO_START_OLLAMA": "sometimes"})


# ---------------------------------------------------------------------------
# Mandatory deterministic Hypothesis PBTs
# ---------------------------------------------------------------------------

_SAFE_KEY_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
_SAFE_VAL_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"

_secret_key_st = st.text(alphabet=_SAFE_KEY_CHARS, min_size=3, max_size=24).filter(
    lambda k: k[0].isalpha()
)
_secret_val_st = st.text(alphabet=_SAFE_VAL_CHARS, min_size=10, max_size=48)
_secrets_dict_st = st.dictionaries(
    keys=_secret_key_st,
    values=_secret_val_st,
    min_size=1,
    max_size=5,
)


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    secrets_map=_secrets_dict_st,
    flip_index=st.integers(min_value=0, max_value=10_000),
    flip_mask=st.integers(min_value=1, max_value=255),
)
def test_pbt_vault_roundtrip_zero_plaintext_and_1byte_tamper_fails_closed(
    secrets_map: dict[str, str],
    flip_index: int,
    flip_mask: int,
) -> None:
    protector = PBKDF2FernetProtector("pbt-deterministic-seed", iterations=1_000)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault_file = Path(tmpdir) / "pbt.vault.enc"
        vault = EncryptedCredentialVault(vault_file, protector=protector)
        vault.set_many(secrets_map)

        # 1. Exact round-trip fidelity
        assert vault.load_all() == secrets_map

        # 2. Zero plaintext secret substrings anywhere in raw file bytes
        raw_bytes = vault_file.read_bytes()
        assert b"\r\n" not in raw_bytes
        for secret_val in secrets_map.values():
            assert secret_val.encode("utf-8") not in raw_bytes

        # 3. Flipping ANY single byte in the ciphertext blob ALWAYS raises VaultDecryptionError
        envelope = json.loads(raw_bytes.decode("utf-8"))
        ct_bytes = bytearray(base64.b64decode(envelope["ciphertext"]))
        idx = flip_index % len(ct_bytes)
        ct_bytes[idx] ^= flip_mask
        envelope["ciphertext"] = base64.b64encode(bytes(ct_bytes)).decode("ascii")
        vault_file.write_text(json.dumps(envelope) + "\n", encoding="utf-8")

        with pytest.raises(VaultDecryptionError):
            vault.load_all()


@settings(derandomize=True, max_examples=50, deadline=None)
@given(secret=st.text(min_size=1, max_size=128))
def test_pbt_mask_secret_never_reveals_full_input(secret: str) -> None:
    masked = mask_secret(secret)
    assert masked != secret
    assert len(masked) > 0


@settings(derandomize=True, max_examples=50, deadline=None)
@given(
    prefix=st.text(alphabet=_SAFE_VAL_CHARS, min_size=4, max_size=4),
    middle=st.text(alphabet=_SAFE_VAL_CHARS, min_size=1, max_size=40),
    suffix=st.text(alphabet=_SAFE_VAL_CHARS, min_size=4, max_size=4),
)
def test_pbt_mask_secret_never_leaks_middle_characters(
    prefix: str,
    middle: str,
    suffix: str,
) -> None:
    secret = f"{prefix}{middle}{suffix}"
    masked = mask_secret(secret)
    assert masked == f"{prefix}...{suffix}"
    assert masked != secret
    # Varying the middle bytes produces the exact same masked string (0 bits leaked from middle)
    alt_middle = "Z" * (len(middle) + 3) if middle != "Z" * len(middle) else "A" * (len(middle) + 3)
    assert mask_secret(f"{prefix}{alt_middle}{suffix}") == masked


def test_verify_credential_live_probes_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline MockTransport verification of verify_credential_live across Gemini, Groq, and non-probed keys."""
    import httpx
    from reqhq.security import verify_credential_live
    import reqhq.security.credential_vault as cv_mod

    seen: list[tuple[str, dict[str, str]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((str(req.url), dict(req.headers)))
        if req.headers.get("x-goog-api-key") == "good-gemini":
            return httpx.Response(200, json={"models": []})
        if req.headers.get("authorization") == "Bearer bad-groq":
            return httpx.Response(401, text="invalid api key")
        if req.headers.get("x-goog-api-key") == "err-500":
            return httpx.Response(500, text="server error")
        raise httpx.ConnectError("offline", request=req)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    ok_g, msg_g = verify_credential_live("GEMINI_API_KEY", "good-gemini", client=client)
    assert ok_g is True and "HTTP 200" in msg_g

    ok_q, msg_q = verify_credential_live("GROQ_API_KEY", "bad-groq", client=client)
    assert ok_q is False and "HTTP 401" in msg_q

    ok_500, msg_500 = verify_credential_live("GEMINI_API_KEY", "err-500", client=client)
    assert ok_500 is False and "HTTP 500" in msg_500

    ok_net, msg_net = verify_credential_live("GEMINI_API_KEY", "net-fail", client=client)
    assert ok_net is False and "request failed" in msg_net

    ok_other, msg_other = verify_credential_live("MSAL_CLIENT_ID", "some-uuid", client=client)
    assert ok_other is True and "no live probe" in msg_other

    # Exercise the default client=None context-manager path offline via monkeypatched httpx.Client
    class _FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> _FakeHttpClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            assert "generativelanguage.googleapis.com" in url
            assert headers["x-goog-api-key"] == "ctx-gemini"
            return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(cv_mod.httpx, "Client", _FakeHttpClient)
    ok_ctx, msg_ctx = verify_credential_live("GEMINI_API_KEY", "ctx-gemini")
    assert ok_ctx is True and "HTTP 200" in msg_ctx
