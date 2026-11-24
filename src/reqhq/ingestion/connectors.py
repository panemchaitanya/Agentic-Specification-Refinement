"""Local and Day 1 read-only cloud connectors for reqhq ingestion."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt", ".pdf", ".docx"})

EXTENSION_TO_MEDIA_TYPE: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MEDIA_TYPE_TO_EXTENSION: dict[str, str] = {
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

GDRIVE_READONLY_SCOPE: str = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DOC_MIME: str = "application/vnd.google-apps.document"

GDRIVE_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "md": ("text/markdown", ".md"),
    "markdown": ("text/markdown", ".md"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "txt": ("text/plain", ".txt"),
    "plain": ("text/plain", ".txt"),
    "pdf": ("application/pdf", ".pdf"),
}

SHAREPOINT_READONLY_SCOPE: str = "Files.Read.All"
SHAREPOINT_ALLOWED_SCOPES: frozenset[str] = frozenset(
    {
        "Files.Read.All",
        "https://graph.microsoft.com/Files.Read.All",
    }
)
GRAPH_BASE_URL: str = "https://graph.microsoft.com/v1.0"


class ConnectorError(RuntimeError):
    """Raised when a local or cloud connector operation fails."""


class ScopeViolationError(PermissionError):
    """Raised when a cloud connector is configured with a non-read-only scope."""


class UnsupportedFileTypeError(ValueError):
    """Raised when an input document has an unsupported extension or media type."""


def compute_sha256(content: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


def infer_media_type(extension: str) -> str:
    """Return the canonical MIME media type for a supported extension."""
    ext = extension.lower()
    if ext not in EXTENSION_TO_MEDIA_TYPE:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension {extension!r}; supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    return EXTENSION_TO_MEDIA_TYPE[ext]


def _resolve_supported_extension(filename: str, media_type: str | None = None) -> str:
    """Resolve and validate a supported lowercase extension from filename or MIME type."""
    ext = Path(filename).suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return ext
    if media_type:
        inferred = MEDIA_TYPE_TO_EXTENSION.get(media_type.lower().split(";")[0].strip())
        if inferred in SUPPORTED_EXTENSIONS:
            return inferred
    raise UnsupportedFileTypeError(
        f"Unsupported file type for {filename!r} (media_type={media_type!r}); "
        f"supported extensions: {sorted(SUPPORTED_EXTENSIONS)}"
    )


class SourceDocument(BaseModel):
    """Raw source document bytes and provenance loaded by an ingestion connector."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., min_length=1)
    source_uri: str = Field(..., min_length=1)
    source_type: Literal["local", "gdrive", "sharepoint"]
    media_type: str = Field(..., min_length=1)
    extension: str = Field(..., pattern=r"^\.[a-z0-9]+$")
    content: bytes
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(..., ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalFileLoader:
    """Loads local `.md`, `.txt`, `.pdf`, and `.docx` files with SHA-256 hashing."""

    def __init__(self, supported_extensions: frozenset[str] = SUPPORTED_EXTENSIONS) -> None:
        self.supported_extensions = frozenset(ext.lower() for ext in supported_extensions)

    def load_file(self, path: Path | str) -> SourceDocument:
        """Load a single local file, computing its SHA-256 digest."""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Source file not found: {file_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Expected a file but found a directory: {file_path}")

        ext = file_path.suffix.lower()
        if ext not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"Unsupported file extension {ext!r} for {file_path.name!r}; "
                f"supported: {sorted(self.supported_extensions)}"
            )

        data = file_path.read_bytes()
        resolved = file_path.resolve()
        return SourceDocument(
            filename=file_path.name,
            source_uri=resolved.as_uri(),
            source_type="local",
            media_type=infer_media_type(ext),
            extension=ext,
            content=data,
            sha256=compute_sha256(data),
            size_bytes=len(data),
            metadata={"local_path": str(resolved)},
        )

    def load_directory(
        self,
        directory: Path | str,
        *,
        recursive: bool = True,
        strict_extensions: bool = False,
    ) -> list[SourceDocument]:
        """Load supported documents from a directory (recursively by default)."""
        dir_path = Path(directory)
        if not dir_path.exists():
            raise FileNotFoundError(f"Source directory not found: {dir_path}")
        if not dir_path.is_dir():
            raise NotADirectoryError(f"Expected a directory: {dir_path}")

        iterator = dir_path.rglob("*") if recursive else dir_path.glob("*")
        files = sorted(p for p in iterator if p.is_file())
        documents: list[SourceDocument] = []
        for file_path in files:
            ext = file_path.suffix.lower()
            if ext not in self.supported_extensions:
                if strict_extensions:
                    raise UnsupportedFileTypeError(
                        f"Unsupported file extension {ext!r} for {file_path.name!r}; "
                        f"supported: {sorted(self.supported_extensions)}"
                    )
                continue
            documents.append(self.load_file(file_path))
        return documents

    def load_path(
        self,
        path: Path | str,
        *,
        recursive: bool = True,
        strict_extensions: bool = False,
    ) -> list[SourceDocument]:
        """Load either a single file or a directory of supported documents."""
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError(f"Source path not found: {target}")
        if target.is_dir():
            return self.load_directory(
                target,
                recursive=recursive,
                strict_extensions=strict_extensions,
            )
        return [self.load_file(target)]


class GDriveConnector:
    """Day 1 read-only Google Drive OAuth2 connector.

    Strictly enforces the ``https://www.googleapis.com/auth/drive.readonly`` scope.
    Supports injecting a mock or pre-built Drive v3 service for offline unit testing.
    """

    REQUIRED_SCOPE: str = GDRIVE_READONLY_SCOPE

    def __init__(
        self,
        *,
        scopes: Sequence[str] = (GDRIVE_READONLY_SCOPE,),
        client_secrets_path: Path | str | None = None,
        token_path: Path | str | None = None,
        service: Any | None = None,
    ) -> None:
        self.scopes = self._validate_scopes(scopes)
        self.client_secrets_path = Path(client_secrets_path) if client_secrets_path else None
        self.token_path = Path(token_path) if token_path else None
        self._service = service

    @classmethod
    def _validate_scopes(cls, scopes: Sequence[str]) -> tuple[str, ...]:
        scope_list = tuple(s.strip() for s in scopes if s and s.strip())
        if not scope_list:
            raise ScopeViolationError(
                f"GDriveConnector requires exactly the read-only scope {cls.REQUIRED_SCOPE!r}."
            )
        disallowed = [s for s in scope_list if s != cls.REQUIRED_SCOPE]
        if disallowed:
            raise ScopeViolationError(
                f"GDriveConnector strictly enforces read-only scope {cls.REQUIRED_SCOPE!r}; "
                f"rejected scopes: {disallowed}"
            )
        return (cls.REQUIRED_SCOPE,)

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds: Any | None = None
        if self.token_path and self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), list(self.scopes)
            )
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.client_secrets_path or not self.client_secrets_path.exists():
                    raise ConnectorError(
                        "Google Drive OAuth2 client secrets file is required when no service "
                        "or valid token is supplied."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secrets_path), list(self.scopes)
                )
                creds = flow.run_local_server(port=0)
            if self.token_path:
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                self.token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("drive", "v3", credentials=creds)
        return self._service

    def list_folder(self, folder_id: str) -> list[dict[str, Any]]:
        """List non-trashed files inside a Google Drive folder (with pagination)."""
        service = self._get_service()
        safe_folder_id = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        query = f"'{safe_folder_id}' in parents and trashed = false"
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "q": query,
                "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, size)",
            }
            if page_token is not None:
                kwargs["pageToken"] = page_token
            response = service.files().list(**kwargs).execute()
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files

    def fetch_file(
        self,
        file_id: str,
        *,
        export_format: Literal["md", "markdown", "docx", "txt", "plain", "pdf"] = "md",
    ) -> SourceDocument:
        """Download a binary Drive file or export a Google Doc into a SourceDocument."""
        service = self._get_service()
        meta = (
            service.files()
            .get(fileId=file_id, fields="id, name, mimeType, modifiedTime, size")
            .execute()
        )
        name = str(meta.get("name") or f"{file_id}")
        mime_type = str(meta.get("mimeType") or "")

        if mime_type == GOOGLE_DOC_MIME:
            fmt_key = export_format.lower()
            if fmt_key not in GDRIVE_EXPORT_FORMATS:
                raise UnsupportedFileTypeError(
                    f"Unsupported Google Docs export format {export_format!r}; "
                    f"supported: {sorted(GDRIVE_EXPORT_FORMATS)}"
                )
            target_mime, ext = GDRIVE_EXPORT_FORMATS[fmt_key]
            raw = service.files().export_media(fileId=file_id, mimeType=target_mime).execute()
            content = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
            filename = name if name.lower().endswith(ext) else f"{name}{ext}"
            media_type = target_mime
            exported = True
        else:
            ext = _resolve_supported_extension(name, mime_type or None)
            raw = service.files().get_media(fileId=file_id).execute()
            content = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
            filename = name if name.lower().endswith(ext) else f"{name}{ext}"
            media_type = infer_media_type(ext)
            exported = False

        return SourceDocument(
            filename=filename,
            source_uri=f"gdrive://{file_id}",
            source_type="gdrive",
            media_type=media_type,
            extension=ext,
            content=content,
            sha256=compute_sha256(content),
            size_bytes=len(content),
            metadata={
                "gdrive_file_id": file_id,
                "original_mime_type": mime_type,
                "exported_from_google_doc": exported,
                "modified_time": meta.get("modifiedTime"),
                "scopes": list(self.scopes),
            },
        )

    def fetch_folder(
        self,
        folder_id: str,
        *,
        export_format: Literal["md", "markdown", "docx", "txt", "plain", "pdf"] = "md",
        skip_unsupported: bool = True,
    ) -> list[SourceDocument]:
        """Download all supported files from a Google Drive folder."""
        items = self.list_folder(folder_id)
        documents: list[SourceDocument] = []
        for item in items:
            item_id = str(item["id"])
            try:
                documents.append(self.fetch_file(item_id, export_format=export_format))
            except UnsupportedFileTypeError:
                if not skip_unsupported:
                    raise
        return documents


class SharePointConnector:
    """Day 1 read-only OneDrive/SharePoint connector powered by MSAL + Microsoft Graph.

    Strictly enforces the ``Files.Read.All`` read-only scope. Accepts injectable
    ``msal_app`` and ``http_client`` instances for 100% offline unit testing.
    """

    REQUIRED_SCOPE: str = SHAREPOINT_READONLY_SCOPE
    ALLOWED_SCOPES: frozenset[str] = SHAREPOINT_ALLOWED_SCOPES

    def __init__(
        self,
        *,
        client_id: str = "",
        tenant_id: str = "common",
        scopes: Sequence[str] = (SHAREPOINT_READONLY_SCOPE,),
        msal_app: Any | None = None,
        http_client: httpx.Client | None = None,
        graph_base_url: str = GRAPH_BASE_URL,
    ) -> None:
        self.scopes = self._validate_scopes(scopes)
        self.client_id = client_id
        self.tenant_id = tenant_id
        self._msal_app = msal_app
        self._http_client = http_client
        self.graph_base_url = graph_base_url.rstrip("/")

    @classmethod
    def _validate_scopes(cls, scopes: Sequence[str]) -> tuple[str, ...]:
        scope_list = tuple(s.strip() for s in scopes if s and s.strip())
        if not scope_list:
            raise ScopeViolationError(
                f"SharePointConnector requires read-only scope {cls.REQUIRED_SCOPE!r}."
            )
        disallowed = [s for s in scope_list if s not in cls.ALLOWED_SCOPES]
        if disallowed:
            raise ScopeViolationError(
                f"SharePointConnector strictly enforces read-only scope {cls.REQUIRED_SCOPE!r}; "
                f"rejected scopes: {disallowed}"
            )
        return scope_list

    def _get_msal_app(self) -> Any:
        if self._msal_app is not None:
            return self._msal_app
        if not self.client_id:
            raise ConnectorError(
                "MSAL client_id is required when no pre-built msal_app is injected."
            )
        import msal

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self._msal_app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=authority,
        )
        return self._msal_app

    def acquire_access_token(self) -> str:
        """Acquire a Microsoft Graph bearer token using MSAL."""
        app = self._get_msal_app()
        scopes = list(self.scopes)

        result: dict[str, Any] | None = None
        accounts = app.get_accounts() if hasattr(app, "get_accounts") else []
        if accounts and hasattr(app, "acquire_token_silent"):
            result = app.acquire_token_silent(scopes, account=accounts[0])

        if not result or "access_token" not in result:
            if hasattr(app, "acquire_token_interactive"):
                result = app.acquire_token_interactive(scopes=scopes)
            else:
                raise ConnectorError(
                    "MSAL application failed silent acquisition and lacks interactive acquisition."
                )

        if not result or "access_token" not in result:
            err = (result or {}).get("error_description") or (result or {}).get("error") or "unknown"
            raise ConnectorError(f"Failed to acquire Microsoft Graph access token: {err}")
        return str(result["access_token"])

    def _drive_prefix(self, *, site_id: str | None = None, drive_id: str | None = None) -> str:
        if drive_id:
            return f"{self.graph_base_url}/drives/{drive_id}"
        if site_id:
            return f"{self.graph_base_url}/sites/{site_id}/drive"
        return f"{self.graph_base_url}/me/drive"

    def _request_with_client(self, method: str, url: str, headers: dict[str, str]) -> httpx.Response:
        if method.upper() != "GET":
            raise ScopeViolationError(
                f"SharePointConnector is strictly read-only and forbids HTTP {method.upper()} requests."
            )
        if self._http_client is not None:
            response = self._http_client.request(method, url, headers=headers)
            response.raise_for_status()
            return response
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            response = client.request(method, url, headers=headers)
            response.raise_for_status()
            return response

    def list_drive_children(
        self,
        folder_id: str = "root",
        *,
        site_id: str | None = None,
        drive_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List child items in a OneDrive/SharePoint drive folder (following pagination)."""
        token = self.acquire_access_token()
        prefix = self._drive_prefix(site_id=site_id, drive_id=drive_id)
        if folder_id == "root":
            url: str | None = f"{prefix}/root/children"
        else:
            url = f"{prefix}/items/{folder_id}/children"
        headers = {"Authorization": f"Bearer {token}"}
        items: list[dict[str, Any]] = []
        while url:
            response = self._request_with_client("GET", url, headers=headers)
            payload = response.json()
            items.extend(payload.get("value", []))
            next_link = payload.get("@odata.nextLink")
            url = str(next_link) if next_link else None
        return items

    def fetch_drive_item(
        self,
        item_id: str,
        *,
        site_id: str | None = None,
        drive_id: str | None = None,
    ) -> SourceDocument:
        """Download a supported OneDrive/SharePoint drive item via Microsoft Graph."""
        token = self.acquire_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        prefix = self._drive_prefix(site_id=site_id, drive_id=drive_id)
        item_url = f"{prefix}/items/{item_id}"

        meta_resp = self._request_with_client("GET", item_url, headers=headers)
        meta = meta_resp.json()
        raw_name = str(meta.get("name") or item_id)
        file_info = meta.get("file") or {}
        raw_mime = file_info.get("mimeType")
        ext = _resolve_supported_extension(raw_name, raw_mime)
        filename = raw_name if raw_name.lower().endswith(ext) else f"{raw_name}{ext}"

        download_url = meta.get("@microsoft.graph.downloadUrl")
        if download_url:
            content_resp = self._request_with_client("GET", str(download_url), headers={})
        else:
            content_resp = self._request_with_client(
                "GET", f"{item_url}/content", headers=headers
            )

        content = bytes(content_resp.content)
        source_uri = str(meta.get("webUrl") or f"sharepoint://{item_id}")

        return SourceDocument(
            filename=filename,
            source_uri=source_uri,
            source_type="sharepoint",
            media_type=infer_media_type(ext),
            extension=ext,
            content=content,
            sha256=compute_sha256(content),
            size_bytes=len(content),
            metadata={
                "item_id": item_id,
                "site_id": site_id,
                "drive_id": drive_id,
                "scopes": list(self.scopes),
            },
        )
