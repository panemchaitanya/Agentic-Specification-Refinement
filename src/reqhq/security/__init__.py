"""Public security and encrypted credential vault APIs for reqhq v2."""

from __future__ import annotations

from reqhq.security.credential_vault import (
    CRYPTPROTECT_UI_FORBIDDEN,
    VAULT_ENVELOPE_VERSION,
    CredentialProvisioningError,
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
    verify_credential_live,
)
from reqhq.security.models import (
    CREDENTIAL_SPECS,
    CredentialCheckReport,
    CredentialSource,
    CredentialSpec,
    CredentialStatusItem,
)

__all__ = [
    "CREDENTIAL_SPECS",
    "CRYPTPROTECT_UI_FORBIDDEN",
    "CredentialCheckReport",
    "CredentialProvisioningError",
    "CredentialSource",
    "CredentialSpec",
    "CredentialStatusItem",
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
