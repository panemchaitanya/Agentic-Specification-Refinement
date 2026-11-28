"""Decode an uploaded PRD file for the UI (plain text and Markdown only)."""

MAX_UPLOAD_BYTES = 200_000  # a PRD is prose; this stops accidental huge or binary files


def decode_prd_upload(data: bytes, name: str = ""):
    """Return (text, error). Exactly one of the two is None.

    Accepts UTF-8 (with or without BOM) text. Rejects empty, oversized and
    binary content with a message the UI can show as-is.
    """
    if not data or not data.strip():
        return None, "The file is empty."
    if len(data) > MAX_UPLOAD_BYTES:
        return None, (f"The file is larger than {MAX_UPLOAD_BYTES // 1000} KB. "
                      "Paste the relevant part of the description instead.")
    if b"\x00" in data:
        return None, "This looks like a binary file. Upload a .txt or .md file."
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "The file is not UTF-8 text. Save it as UTF-8 and try again."
    return text.replace("\r\n", "\n").replace("\r", "\n").strip(), None
