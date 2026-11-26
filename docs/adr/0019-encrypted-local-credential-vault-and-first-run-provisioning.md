# 0019. Encrypted Local Credential Vault and First-Run Provisioning

- **Date:** 2026-11-25
- **Status:** Accepted
- **PR / Branch:** `adr/0019-encrypted-local-credential-vault-and-first-run-provisioning`
- **Supersedes:** None

## Context

ReqHQ v2 runs local-first (ADR-0003) but supports gated free-tier cloud escalation (`GEMINI_API_KEY`, `GROQ_API_KEY`) and optional Day 1 cloud document connectors (`GOOGLE_OAUTH_CLIENT_SECRETS`, `MSAL_CLIENT_ID`, `MSAL_TENANT_ID`, ADR-0004). Relying solely on manually edited plaintext `.env` files or ad-hoc shell environment variables introduces three security and usability hazards:

1. **Plaintext Secrets at Rest:** Storing API keys in unencrypted `.env` files increases the risk of accidental git commits, unencrypted disk leakage, or accidental exposure in diagnostic output and terminal logs.
2. **Template Placeholder Failures:** Users who copy `.env.example` without replacing sample values (such as `your-gemini-api-key-here` or `your-groq-api-key-here`) encounter cryptic HTTP 401/403 failures deep inside multi-stage elicitation or evaluation runs rather than immediate validation and guided recovery.
3. **Passphrase Friction vs. Offline Privacy:** Requiring a manual vault master passphrase on every CLI invocation degrades Windows 11 developer ergonomics, yet silently prompting on stdin during `--strict-local`, `--mock`, or non-interactive CI runs would either violate zero-egress privacy (ADR-0003) or hang automated pipelines.

## Decision

We implement an **Encrypted Local Credential Vault and Interactive First-Run Provisioning Subsystem** under `src/reqhq/security/` (`models.py`, `credential_vault.py`, `__init__.py`), configured via `ReqHQConfig.vault_path` (`REQHQ_VAULT_PATH`, default `.reqhq_sessions/credentials.vault.enc`) in `src/reqhq/config.py`, and exposed via `reqhq auth {setup,status,rotate,clear}` and live-command startup preflight in `src/reqhq/cli.py`:

1. **Zero-Plaintext-on-Disk Envelope Encryption (`EncryptedCredentialVault`):**
   - `src/reqhq/security/credential_vault.py` encrypts the serialized secrets dictionary using `cryptography.fernet.Fernet` (AES-128-CBC confidentiality + HMAC-SHA256 authentication) under a freshly generated per-write Fernet key, then wraps that key via the active `SecretProtector`.
   - The vault writes an atomic, LF-normalized JSON envelope (`{version: 1, backend, ciphertext, master_key_blob}`) to `vault_path` (`0o600` file permissions on POSIX), guaranteeing that raw vault bytes never contain plaintext credential substrings. Repository `.gitignore` ignores `*.vault.enc` and `.reqhq/`.
2. **Windows 11 Native DPAPI & Portable `PBKDF2HMAC(SHA256)` Fallback (`SecretProtector`):**
   - `WindowsDPAPIProtector` invokes native Windows Data Protection API via Python stdlib `ctypes.windll.crypt32.CryptProtectData` and `ctypes.windll.crypt32.CryptUnprotectData` with `CRYPTPROTECT_UI_FORBIDDEN` (`0x01`), binding decryption transparently to the current Windows 11 user account with zero passphrases and zero extra packages.
   - `PBKDF2FernetProtector` derives a 32-byte Fernet key via `PBKDF2HMAC(SHA256, 600_000 iterations)` with a per-payload random 16-byte salt for non-Windows hosts, headless Linux containers, and deterministic offline tests. `default_secret_protector()` selects `WindowsDPAPIProtector` on `sys.platform == "win32"` and `PBKDF2FernetProtector` elsewhere.
3. **Tamper-Evident Fail-Closed Decryption & Strict Secret Masking:**
   - `EncryptedCredentialVault.load_all()` fails closed by raising `VaultDecryptionError` on any 1-byte tamper in `ciphertext` or `master_key_blob`, corrupted JSON, unknown envelope version, or protector failure—never silently swallowing corruption or returning partial secrets.
   - `mask_secret()` guarantees raw secrets are never printed in CLI commands (`reqhq auth status`, `reqhq doctor`) or logs: empty strings return `""`, secrets with `len <= 8` return `"****"`, and secrets with `len > 8` return `f"{value[:4]}...{value[-4:]}"` without leaking middle bytes.
4. **Placeholder Rejection & Input Validation (`is_valid_credential_value`, `src/reqhq/security/models.py`):**
   - `src/reqhq/security/models.py` defines strict `ConfigDict(extra="forbid")` models (`CREDENTIAL_SPECS`, `CredentialSource`, `CredentialStatusItem`, `CredentialCheckReport`).
   - `is_valid_credential_value()` rejects empty/whitespace strings, embedded newlines or control characters, known template placeholders (`your-gemini-api-key-here`, `your-groq-api-key-here`, `00000000-0000-0000-0000-000000000000`, `path/to/credentials.json`, `changeme`, `replace-me`, `xxx`), and under-length API keys (`< 8` characters for `GEMINI_API_KEY` / `GROQ_API_KEY`).
5. **Interactive First-Run Provisioning, Invalid-Key Re-Prompting, & `reqhq auth` Sub-App:**
   - Exposes four management commands in `src/reqhq/cli.py`: `reqhq auth setup`, `reqhq auth status`, `reqhq auth rotate`, and `reqhq auth clear`, all reading user secrets via masked input (`typer.prompt(..., hide_input=True)`) and displaying masked statuses only.
   - At startup of live cloud-enabled commands (`elicit`, `finalize`, `export`, `handoff`, `eval run --mode live`), `ensure_credentials_ready()` resolves each required key in precedence order: valid `ENV` (`CredentialSource.ENV`) -> valid decrypted `VAULT` (`CredentialSource.VAULT`, exported to process `os.environ`) -> interactive secure prompt (`CredentialSource.PROMPTED`, re-prompting on missing, invalid, or placeholder entries and persisting encrypted to the vault so subsequent runs do not re-prompt).
6. **Strict Bypass Under `--strict-local`, `--mock`, and Non-Interactive CI:**
   - Under `--strict-local`, `ensure_credentials_ready(strict_local=True)` never prompts for or requires cloud credentials and returns `all_required_ready=True`, upholding ADR-0003 zero-egress privacy.
   - Under `--mock`, startup credential preflight is bypassed completely. In non-interactive CI (`interactive=False`), missing or invalid live cloud keys never block on stdin; instead they mark `CredentialSource.MISSING` / `CredentialSource.INVALID` (`all_required_ready=False`) so callers fail closed or degrade cleanly according to mode.

## Consequences

- **Positive:** Eliminates plaintext API keys on disk on Windows 11 while providing frictionless, zero-passphrase local encryption bound to the current OS user via native DPAPI and authenticated Fernet encryption.
- **Positive:** Catches `.env.example` placeholder strings (`your-gemini-api-key-here`) and malformed keys at startup before wasting local SLM compute or triggering opaque mid-pipeline cloud auth errors.
- **Positive:** Strict bypass semantics preserve deterministic, zero-prompt offline CI execution and airtight `--strict-local` privacy guarantees.
- **Trade-off:** DPAPI-protected `.vault.enc` files are cryptographically tied to the originating Windows user profile and machine; moving a workspace to a different machine or user account requires running `reqhq auth setup` (or `reqhq auth clear` followed by `setup`) rather than copying the vault file.
- **Obligation:** All tests for `src/reqhq/security/**` and `src/reqhq/config.py` must achieve 100% line coverage and include deterministic Hypothesis property-based tests confirming round-trip fidelity, zero plaintext substrings in raw vault bytes, fail-closed `VaultDecryptionError` on any single-byte ciphertext mutation, and non-leaking `mask_secret()` output.
