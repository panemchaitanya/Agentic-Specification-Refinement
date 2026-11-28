"""Tests for the PRD upload decoder used by the Streamlit UI."""
from specpipe.upload import MAX_UPLOAD_BYTES, decode_prd_upload


def test_plain_text_is_decoded_and_newlines_normalised():
    text, err = decode_prd_upload(b"Users can sign up.\r\nThey can check out.\r\n", "prd.txt")
    assert err is None
    assert text == "Users can sign up.\nThey can check out."


def test_utf8_bom_is_stripped():
    text, err = decode_prd_upload("\ufeffCaf\u00e9 app".encode("utf-8"), "prd.md")
    assert err is None and text == "Caf\u00e9 app"


def test_empty_and_whitespace_only_rejected():
    for data in (b"", b"  \n\t "):
        text, err = decode_prd_upload(data)
        assert text is None and "empty" in err


def test_oversized_rejected():
    text, err = decode_prd_upload(b"a" * (MAX_UPLOAD_BYTES + 1))
    assert text is None and "larger than" in err


def test_binary_and_non_utf8_rejected():
    text, err = decode_prd_upload(b"%PDF-1.7\x00\x01binary")
    assert text is None and "binary" in err
    text, err = decode_prd_upload("caf\u00e9".encode("latin-1"))
    assert text is None and "UTF-8" in err
