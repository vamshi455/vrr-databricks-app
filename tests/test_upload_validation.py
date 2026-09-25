"""What the upload gate must refuse — the adversarial cases, not the happy path.

`core/upload_validation.py` is the only thing standing between a browser and the
filesystem the API process can write to, so the cases below are the attacks it exists to
stop rather than a coverage exercise. Pure module, no DB, no stack: `make test`.
"""
from __future__ import annotations

import io
import random
import zipfile

import pytest

from vrr_agent_open.core import upload_validation as UV

PDF = b"%PDF-1.7\n" + b"x" * 2000
TXT = b"Injection above the fracture gradient is prohibited.\n" * 20


def docx_bytes(entries: dict[str, bytes] | None = None) -> bytes:
    """A minimal but structurally real .docx."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document>procedure text</document>")
        for name, data in (entries or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


# ------------------------------------------------------------- happy path ----
@pytest.mark.parametrize("name,data,kind", [
    ("procedure.pdf", PDF, "pdf"),
    ("notes.txt", TXT, "text"),
    ("readme.md", TXT, "text"),
    ("page.html", b"<html><body>injection limits</body></html>" * 5, "html"),
    ("rows.csv", b"completion,rate\nC-1,120\n" * 20, "csv"),
    ("guide.docx", docx_bytes(), "docx"),
])
def test_accepts_every_supported_kind(name, data, kind):
    v = UV.validate_upload(name, data)
    assert v.ok, v.errors
    assert v.kind == kind
    assert v.sha256 and len(v.sha256) == 64


def test_allowlist_is_a_subset_of_what_the_loaders_can_parse():
    """Accepting a suffix no loader handles would queue a document that can never embed."""
    from vrr_agent_open.pipeline import document_loaders as DL

    assert set(UV.ALLOWED_SUFFIXES) <= set(DL.SUFFIX_LOADERS)


# ---------------------------------------------------------------- filename ----
@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd.txt", "passwd.txt"),          # traversal reduced to the basename
    ("C:\\Users\\a\\report.pdf", "report.pdf"),      # windows separators, on posix
    ("/absolute/path/x.pdf", "x.pdf"),
    ("we ird;name&$.txt", "we ird_name__.txt"),      # shell metacharacters neutralised
    ("....pdf", "pdf"),                              # dot runs collapse
])
def test_safe_filename_cannot_escape_a_directory(raw, expected):
    out = UV.safe_filename(raw)
    assert out == expected
    assert "/" not in out and "\\" not in out and ".." not in out


def test_nul_byte_in_filename_is_stripped():
    assert "\x00" not in UV.safe_filename("evil\x00.pdf")


def test_overlong_filename_is_truncated_but_keeps_its_suffix():
    out = UV.safe_filename("a" * 400 + ".pdf")
    assert len(out) <= UV.MAX_FILENAME_CHARS
    assert out.endswith(".pdf")


def test_empty_filename_is_rejected_not_defaulted():
    v = UV.validate_upload("///", PDF)
    assert not v.ok


# ------------------------------------------------------------------- types ----
@pytest.mark.parametrize("name", [
    "payload.exe", "script.sh", "archive.zip", "sheet.xlsx", "legacy.doc",
    "image.png", "noextension", "config.yaml",
])
def test_disallowed_extensions_are_refused(name):
    v = UV.validate_upload(name, TXT)
    assert not v.ok
    assert "not accepted" in v.reason


def test_double_extension_is_decided_by_the_last_suffix():
    """`x.exe.pdf` is a .pdf claim — and the magic check is what actually catches it."""
    v = UV.validate_upload("payload.exe.pdf", b"MZ" + b"\x00" * 500)
    assert not v.ok
    assert "executable" in v.reason


def test_extension_lying_about_content_is_caught():
    v = UV.validate_upload("report.pdf", b"PK\x03\x04" + b"x" * 500)
    assert not v.ok
    assert "signature" in v.reason or "executable" in v.reason


def test_binary_masquerading_as_text_is_caught():
    v = UV.validate_upload("notes.txt", b"\x00\x01\x02binary" * 100)
    assert not v.ok
    assert "binary" in v.reason


def test_random_bytes_are_not_text_even_without_a_nul():
    """The case that caught the original implementation.

    It tried UTF-8 then fell back to Latin-1 — but Latin-1 assigns all 256 byte values,
    so the decode NEVER fails and the only surviving check was "contains a NUL", which
    random data passes roughly half the time. 200 bytes of /dev/urandom uploaded with a
    201. Constructed here without any NUL so it cannot pass for the wrong reason.
    """
    rng = random.Random(12345)
    blob = bytes(rng.choice([b for b in range(1, 256)]) for _ in range(400))
    assert b"\x00" not in blob
    v = UV.validate_upload("notes.txt", blob)
    assert not v.ok
    assert "binary" in v.reason


def test_high_byte_prose_is_still_accepted():
    """The check must not become 'ASCII only' — accented Latin-1 prose is legitimate."""
    body = "Pression d'injection à la tête de puits: limite fracturation.\n" * 30
    assert UV.validate_upload("note.txt", body.encode("latin-1")).ok


def test_a_document_full_of_tabs_and_newlines_is_text():
    v = UV.validate_upload("table.csv", b"a\tb\tc\r\n1\t2\t3\r\n" * 100)
    assert v.ok, v.errors


def test_latin1_text_is_accepted_because_the_loader_handles_it():
    """`load_text` passes autodetect_encoding=True precisely for field exports."""
    v = UV.validate_upload("export.csv", "well,note\nA-1,café ãéî\n".encode("latin-1") * 20)
    assert v.ok, v.errors


@pytest.mark.parametrize("sig", [b"MZ", b"\x7fELF", b"\x1f\x8b", b"Rar!", b"\xd0\xcf\x11\xe0"])
def test_forbidden_signatures_refused_under_any_extension(sig):
    v = UV.validate_upload("innocent.txt", sig + b"\x00" * 500)
    assert not v.ok


# -------------------------------------------------------------------- size ----
def test_oversize_is_refused_per_kind():
    v = UV.validate_upload("big.txt", b"a" * (UV.MAX_BYTES["text"] + 1))
    assert not v.ok
    assert "limit" in v.reason


def test_empty_file_is_refused():
    assert not UV.validate_upload("empty.pdf", b"").ok


def test_a_pdf_may_be_larger_than_a_text_file():
    """The caps are per kind, and a scanned procedure legitimately dwarfs a note."""
    assert UV.MAX_BYTES["pdf"] > UV.MAX_BYTES["text"]
    size = UV.MAX_BYTES["text"] + 1024
    assert UV.validate_upload("scan.pdf", b"%PDF-1.7\n" + b"x" * size).ok


# --------------------------------------------------------------- zip bombs ----
def test_decompression_bomb_is_refused():
    """A small archive that expands enormously — the classic cheap DoS on any unzipper."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * (50 * 1024 * 1024))   # compresses to ~50 KB
    v = UV.validate_upload("bomb.docx", buf.getvalue())
    assert not v.ok
    assert "ratio" in v.reason or "expands" in v.reason


def test_renamed_zip_is_not_a_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("whatever.txt", "not a word document")
    v = UV.validate_upload("smuggled.docx", buf.getvalue())
    assert not v.ok
    assert "Word document" in v.reason


def test_zip_entry_path_traversal_is_refused():
    v = UV.validate_upload("evil.docx", docx_bytes({"../../escape.xml": b"<x/>"}))
    assert not v.ok
    assert "traversal" in v.reason


def test_corrupt_docx_is_refused_not_crashed():
    v = UV.validate_upload("broken.docx", b"PK\x03\x04" + b"garbage" * 100)
    assert not v.ok


# ------------------------------------------------------------- ergonomics ----
def test_every_failure_is_reported_at_once():
    """One round trip per decision: too big AND wrong type must both come back."""
    v = UV.validate_upload("thing.iso", b"a" * 100)
    assert not v.ok and v.errors


def test_declared_content_type_mismatch_warns_but_does_not_reject():
    """Windows sends application/octet-stream for good PDFs; rejecting on it is wrong."""
    v = UV.validate_upload("real.pdf", PDF, declared_type="application/octet-stream")
    assert v.ok
    assert v.warnings


def test_sanitising_the_name_is_surfaced_to_the_uploader():
    v = UV.validate_upload("my report;v2.pdf", PDF)
    assert v.ok
    assert any("stored as" in w for w in v.warnings)


def test_identical_bytes_hash_identically_under_different_names():
    """The dedupe key. Same content, new filename, must collide."""
    a = UV.validate_upload("a.pdf", PDF)
    b = UV.validate_upload("b.pdf", PDF)
    assert a.sha256 == b.sha256
