"""Encrypted local credential vault and first-run provisioning for reqhq v2."""

from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import secrets
import socket
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from reqhq.security.models import (
    CREDENTIAL_SPECS,
    CredentialCheckReport,
    CredentialSource,
    CredentialStatusItem,
)

CRYPTPROTECT_UI_FORBIDDEN = 0x01
VAULT_ENVELOPE_VERSION = 1
_PBKDF2_SALT_BYTES = 16
_DEFAULT_PBKDF2_ITERATIONS = 600_000
_MAX_PROMPT_ATTEMPTS = 3

_PLACEHOLDER_VALUES = frozenset(
    {
        "your-gemini-api-key-here",
        "your-groq-api-key-here",
        "00000000-0000-0000-0000-000000000000",
        "path/to/credentials.json",
        "changeme",
        "replace-me",
        "xxx",
    }
)


class VaultError(RuntimeError):
    """Base exception for encrypted credential vault failures."""


class VaultDecryptionError(VaultError):
    """Raised fail-closed when vault decryption, integrity, or schema validation fails."""


class CredentialProvisioningError(VaultError):
    """Raised when an invalid credential value or key name is provided."""


def mask_secret(value: str) -> str:
    """Return a masked representation that never reveals the full secret or middle bytes."""
    if not value:
        return ""
    if len(value) <= 8:
        return "<redacted>" if value == "****" else "****"
    masked = f"{value[:4]}...{value[-4:]}"
    if masked == value:
        return f"{value[:4]}***{value[-4:]}"
    return masked


def is_valid_credential_value(key_name: str, value: str) -> tuple[bool, str]:
    """Validate a candidate credential value against format, control-char, and placeholder rules."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False, f"{key_name} must not contain newlines or control characters"

    cleaned = value.strip()
    if not cleaned:
        return False, f"{key_name} must not be empty or whitespace"

    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return False, f"{key_name} is still set to a placeholder value"

    spec = CREDENTIAL_SPECS.get(key_name)
    if spec is not None and len(cleaned) < spec.min_length:
        return False, f"{key_name} must be at least {spec.min_length} characters long"

    return True, "ok"


@runtime_checkable
class SecretProtector(Protocol):
    """Protocol for protecting and unprotecting the vault's per-write Fernet master key."""

    backend_name: str

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class WindowsDPAPIProtector:
    """Current-Windows-user secret protector backed by native Win32 DPAPI (crypt32.dll)."""

    backend_name: str = "windows-dpapi"

    def __init__(
        self,
        *,
        crypt32: Any | None = None,
        kernel32: Any | None = None,
    ) -> None:
        if crypt32 is None or kernel32 is None:
            if sys.platform != "win32":
                raise VaultError("WindowsDPAPIProtector is only supported on Windows (win32)")
            windll = ctypes.windll  # type: ignore[attr-defined]
            crypt32 = windll.crypt32
            kernel32 = windll.kernel32
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_wchar_p,
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptProtectData.restype = ctypes.c_int
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.POINTER(ctypes.c_wchar_p),
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptUnprotectData.restype = ctypes.c_int
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
        self._crypt32 = crypt32
        self._kernel32 = kernel32

    def protect(self, plaintext: bytes) -> bytes:
        """Encrypt bytes using CryptProtectData bound to the current Windows user."""
        buf = ctypes.create_string_buffer(plaintext, len(plaintext))
        in_blob = _DATA_BLOB(len(plaintext), buf)
        out_blob = _DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "reqhq-credential-vault",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise VaultError("Windows DPAPI CryptProtectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            if out_blob.pbData:
                self._kernel32.LocalFree(out_blob.pbData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        """Decrypt bytes previously protected by CryptProtectData."""
        if not ciphertext:
            raise VaultDecryptionError("Empty DPAPI ciphertext blob")
        buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        in_blob = _DATA_BLOB(len(ciphertext), buf)
        out_blob = _DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise VaultDecryptionError("Windows DPAPI CryptUnprotectData failed")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            if out_blob.pbData:
                self._kernel32.LocalFree(out_blob.pbData)


class PBKDF2FernetProtector:
    """Portable secret protector deriving per-payload Fernet keys via PBKDF2-HMAC-SHA256."""

    backend_name: str = "pbkdf2-fernet"

    def __init__(
        self,
        passphrase: str | bytes | None = None,
        *,
        iterations: int = _DEFAULT_PBKDF2_ITERATIONS,
    ) -> None:
        if iterations < 1:
            raise ValueError("iterations must be a positive integer")
        if passphrase is None:
            seed = f"reqhq-vault-v1:{os.name}:{socket.gethostname()}:{getpass.getuser()}:{Path.home()}"
            secret_bytes = seed.encode("utf-8")
        elif isinstance(passphrase, str):
            secret_bytes = passphrase.encode("utf-8")
        else:
            secret_bytes = bytes(passphrase)
        if not secret_bytes:
            raise ValueError("passphrase must not be empty")
        self._secret = secret_bytes
        self._iterations = iterations

    def _derive_fernet_key(self, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self._iterations,
        )
        return base64.urlsafe_b64encode(kdf.derive(self._secret))

    def protect(self, plaintext: bytes) -> bytes:
        """Encrypt bytes with a fresh 16-byte random salt and PBKDF2-derived Fernet key."""
        salt = secrets.token_bytes(_PBKDF2_SALT_BYTES)
        token = Fernet(self._derive_fernet_key(salt)).encrypt(plaintext)
        return salt + token

    def unprotect(self, ciphertext: bytes) -> bytes:
        """Decrypt bytes produced by protect(), raising VaultDecryptionError on any failure."""
        if len(ciphertext) <= _PBKDF2_SALT_BYTES:
            raise VaultDecryptionError("PBKDF2 protected payload is truncated")
        salt = ciphertext[:_PBKDF2_SALT_BYTES]
        token = ciphertext[_PBKDF2_SALT_BYTES:]
        try:
            return Fernet(self._derive_fernet_key(salt)).decrypt(token)
        except (InvalidToken, ValueError, TypeError) as exc:
            raise VaultDecryptionError("Failed to decrypt PBKDF2-protected key blob") from exc


def default_secret_protector() -> SecretProtector:
    """Return WindowsDPAPIProtector on Windows, or PBKDF2FernetProtector on other platforms."""
    if sys.platform == "win32":
        return WindowsDPAPIProtector()
    return PBKDF2FernetProtector()


def _strict_b64decode(raw: object, field_name: str) -> bytes:
    if not isinstance(raw, str) or not raw:
        raise VaultDecryptionError(f"Vault field {field_name!r} must be a non-empty base64 string")
    try:
        decoded = base64.b64decode(raw.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise VaultDecryptionError(f"Vault field {field_name!r} contains invalid base64") from exc
    if base64.b64encode(decoded).decode("ascii") != raw:
        raise VaultDecryptionError(f"Vault field {field_name!r} failed canonical base64 check")
    return decoded


class EncryptedCredentialVault:
    """Atomic, tamper-evident, zero-plaintext local credential vault."""

    def __init__(
        self,
        vault_path: Path | str = Path(".reqhq_sessions") / "credentials.vault.enc",
        *,
        protector: SecretProtector | None = None,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.protector: SecretProtector = (
            protector if protector is not None else default_secret_protector()
        )

    def exists(self) -> bool:
        """Return True if the encrypted vault file currently exists on disk."""
        return self.vault_path.is_file()

    def load_all(self) -> dict[str, str]:
        """Decrypt and return all stored credentials, failing closed on any corruption or tamper."""
        if not self.exists():
            return {}
        try:
            raw_text = self.vault_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise VaultDecryptionError(f"Failed to read vault file {self.vault_path}") from exc

        try:
            envelope = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise VaultDecryptionError("Vault file is not valid JSON") from exc

        if not isinstance(envelope, dict):
            raise VaultDecryptionError("Vault envelope must be a JSON object")

        version = envelope.get("version")
        if isinstance(version, bool) or version != VAULT_ENVELOPE_VERSION:
            raise VaultDecryptionError(f"Unsupported vault envelope version: {version!r}")

        backend = envelope.get("backend")
        if not isinstance(backend, str) or backend != self.protector.backend_name:
            raise VaultDecryptionError(
                f"Vault protector backend mismatch: expected {self.protector.backend_name!r}, got {backend!r}"
            )

        ciphertext = _strict_b64decode(envelope.get("ciphertext"), "ciphertext")
        master_key_blob = _strict_b64decode(envelope.get("master_key_blob"), "master_key_blob")

        try:
            fernet_key = self.protector.unprotect(master_key_blob)
        except VaultDecryptionError:
            raise
        except Exception as exc:
            raise VaultDecryptionError("Protector failed to unprotect master key") from exc

        try:
            plaintext = Fernet(fernet_key).decrypt(ciphertext)
        except (InvalidToken, ValueError, TypeError) as exc:
            raise VaultDecryptionError("Vault ciphertext failed integrity or decryption check") from exc

        try:
            parsed = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultDecryptionError("Decrypted vault payload is not valid JSON") from exc

        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise VaultDecryptionError("Decrypted vault payload must map strings to strings")

        return dict(parsed)

    def _save_all(self, data: Mapping[str, str]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        fernet_key = Fernet.generate_key()
        payload_bytes = json.dumps(
            dict(sorted(data.items())),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        token = Fernet(fernet_key).encrypt(payload_bytes)
        protected_key = self.protector.protect(fernet_key)

        envelope = {
            "version": VAULT_ENVELOPE_VERSION,
            "backend": self.protector.backend_name,
            "ciphertext": base64.b64encode(token).decode("ascii"),
            "master_key_blob": base64.b64encode(protected_key).decode("ascii"),
        }
        serialized = json.dumps(envelope, indent=2, sort_keys=True) + "\n"

        tmp_path = self.vault_path.parent / f".{self.vault_path.name}.{secrets.token_hex(8)}.tmp"
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
        if os.name != "nt":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, self.vault_path)

    def get(self, key_name: str) -> str | None:
        """Return the decrypted secret value for key_name, or None if not present."""
        return self.load_all().get(key_name)

    def set(self, key_name: str, value: str) -> None:
        """Validate, encrypt, and persist a single credential."""
        self.set_many({key_name: value})

    def set_many(self, items: Mapping[str, str]) -> None:
        """Validate all credentials atomically, then encrypt and persist them to the vault."""
        validated: dict[str, str] = {}
        for raw_key, raw_value in items.items():
            key_name = raw_key.strip()
            if not key_name:
                raise CredentialProvisioningError("Credential key name must not be empty")
            ok, reason = is_valid_credential_value(key_name, raw_value)
            if not ok:
                raise CredentialProvisioningError(reason)
            validated[key_name] = raw_value.strip()

        existing = self.load_all()
        existing.update(validated)
        self._save_all(existing)

    def delete(self, key_name: str) -> bool:
        """Remove key_name from the vault if present; return True if removed."""
        if not self.exists():
            return False
        existing = self.load_all()
        if key_name not in existing:
            return False
        del existing[key_name]
        self._save_all(existing)
        return True

    def clear(self) -> None:
        """Delete the encrypted vault file from disk if it exists."""
        if self.vault_path.exists():
            self.vault_path.unlink()

    def list_masked(self) -> dict[str, str]:
        """Return all stored keys with masked values (never raw secrets)."""
        return {k: mask_secret(v) for k, v in sorted(self.load_all().items())}


def _validate_candidate(
    key_name: str,
    value: str,
    validator_fn: Callable[[str, str], tuple[bool, str]] | None,
) -> tuple[bool, str]:
    ok, reason = is_valid_credential_value(key_name, value)
    if not ok:
        return False, reason
    if validator_fn is not None:
        return validator_fn(key_name, value.strip())
    return True, "ok"


def verify_credential_live(
    key_name: str,
    value: str,
    *,
    timeout_seconds: float = 10.0,
    client: httpx.Client | None = None,
) -> tuple[bool, str]:
    """Perform a lightweight, non-destructive live auth probe for supported cloud API keys."""
    if key_name == "GEMINI_API_KEY":
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {"x-goog-api-key": value}
    elif key_name == "GROQ_API_KEY":
        url = "https://api.groq.com/openai/v1/models"
        headers = {"Authorization": f"Bearer {value}"}
    else:
        return True, "ok (no live probe defined)"

    try:
        if client is not None:
            resp = client.get(url, headers=headers, timeout=timeout_seconds)
        else:
            with httpx.Client(timeout=timeout_seconds) as http_client:
                resp = http_client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Live verification request failed: {exc}"

    if resp.status_code == 200:
        return True, "live verified (HTTP 200)"
    if resp.status_code in (401, 403):
        return False, f"Rejected by provider (HTTP {resp.status_code})"
    return False, f"Unexpected provider response (HTTP {resp.status_code})"


def ensure_credentials_ready(
    vault: EncryptedCredentialVault,
    *,
    required_keys: Sequence[str] = ("GEMINI_API_KEY", "GROQ_API_KEY"),
    env: Mapping[str, str] | None = None,
    strict_local: bool = False,
    interactive: bool = False,
    prompt_fn: Callable[[str], str] | None = None,
    validator_fn: Callable[[str, str], tuple[bool, str]] | None = None,
    export_to_env: bool = True,
) -> CredentialCheckReport:
    """Resolve, validate, and optionally provision required credentials via env and vault."""
    if strict_local:
        items = [
            CredentialStatusItem(
                key_name=key_name,
                configured=False,
                valid=True,
                source=CredentialSource.MISSING,
                masked_value="",
                message="Skipped under --strict-local mode (local-only execution)",
            )
            for key_name in required_keys
        ]
        return CredentialCheckReport(
            vault_path=vault.vault_path,
            protector_backend=vault.protector.backend_name,
            strict_local=True,
            items=items,
            all_required_ready=True,
            prompted_keys=[],
        )

    env_source = os.environ if env is None else env
    vault_secrets = vault.load_all()

    items: list[CredentialStatusItem] = []
    prompted_keys: list[str] = []
    to_save: dict[str, str] = {}
    all_ready = True

    for key_name in required_keys:
        invalid_reason: str | None = None
        invalid_masked = ""

        env_raw = env_source.get(key_name)
        if env_raw is not None and env_raw != "":
            ok, reason = _validate_candidate(key_name, env_raw, validator_fn)
            if ok:
                msg = (
                    f"Loaded from environment ({reason})"
                    if validator_fn is not None and reason != "ok"
                    else "Loaded from environment"
                )
                items.append(
                    CredentialStatusItem(
                        key_name=key_name,
                        configured=True,
                        valid=True,
                        source=CredentialSource.ENV,
                        masked_value=mask_secret(env_raw.strip()),
                        message=msg,
                    )
                )
                continue
            invalid_reason = f"Invalid environment value for {key_name}: {reason}"
            invalid_masked = mask_secret(env_raw.strip())

        vault_raw = vault_secrets.get(key_name)
        if vault_raw is not None:
            ok, reason = _validate_candidate(key_name, vault_raw, validator_fn)
            if ok:
                cleaned = vault_raw.strip()
                if export_to_env and env is None:
                    os.environ[key_name] = cleaned
                msg = (
                    f"Loaded from encrypted vault ({reason})"
                    if validator_fn is not None and reason != "ok"
                    else "Loaded from encrypted vault"
                )
                items.append(
                    CredentialStatusItem(
                        key_name=key_name,
                        configured=True,
                        valid=True,
                        source=CredentialSource.VAULT,
                        masked_value=mask_secret(cleaned),
                        message=msg,
                    )
                )
                continue
            invalid_reason = f"Invalid vault value for {key_name}: {reason}"
            invalid_masked = mask_secret(vault_raw.strip())

        resolved = False
        if interactive and prompt_fn is not None:
            for _attempt in range(_MAX_PROMPT_ATTEMPTS):
                try:
                    entered = prompt_fn(key_name)
                except (EOFError, KeyboardInterrupt):
                    break
                if not entered:
                    break
                ok, reason = _validate_candidate(key_name, entered, validator_fn)
                if ok:
                    cleaned = entered.strip()
                    to_save[key_name] = cleaned
                    prompted_keys.append(key_name)
                    if export_to_env and env is None:
                        os.environ[key_name] = cleaned
                    items.append(
                        CredentialStatusItem(
                            key_name=key_name,
                            configured=True,
                            valid=True,
                            source=CredentialSource.PROMPTED,
                            masked_value=mask_secret(cleaned),
                            message="Provisioned interactively and saved to encrypted vault",
                        )
                    )
                    resolved = True
                    break
                invalid_reason = f"Invalid value for {key_name}: {reason}"
                invalid_masked = mask_secret(entered.strip())

        if not resolved:
            all_ready = False
            if invalid_reason is not None:
                items.append(
                    CredentialStatusItem(
                        key_name=key_name,
                        configured=True,
                        valid=False,
                        source=CredentialSource.INVALID,
                        masked_value=invalid_masked,
                        message=invalid_reason,
                    )
                )
            else:
                items.append(
                    CredentialStatusItem(
                        key_name=key_name,
                        configured=False,
                        valid=False,
                        source=CredentialSource.MISSING,
                        masked_value="",
                        message=f"{key_name} is not configured",
                    )
                )

    if to_save:
        vault.set_many(to_save)

    return CredentialCheckReport(
        vault_path=vault.vault_path,
        protector_backend=vault.protector.backend_name,
        strict_local=False,
        items=items,
        all_required_ready=all_ready,
        prompted_keys=prompted_keys,
    )


__all__ = [
    "CRYPTPROTECT_UI_FORBIDDEN",
    "CredentialProvisioningError",
    "EncryptedCredentialVault",
    "PBKDF2FernetProtector",
    "SecretProtector",
    "VAULT_ENVELOPE_VERSION",
    "VaultDecryptionError",
    "VaultError",
    "WindowsDPAPIProtector",
    "default_secret_protector",
    "ensure_credentials_ready",
    "is_valid_credential_value",
    "mask_secret",
    "verify_credential_live",
]
