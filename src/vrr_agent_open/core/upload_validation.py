"""What a browser is allowed to put into the knowledge index — decided here, purely.

`pipeline/knowledge_ingest.py` was written for files an operator dropped into a folder on
their own laptop, so its only check was `suffix in SUFFIX_LOADERS`. The moment an HTTP
endpoint accepts a file, the uploader is no longer the person who owns the machine, and
"which suffixes do we have a loader for" stops being a security boundary.

This module is that boundary. It is PURE — bytes and a filename in, a verdict out, no
disk, no database, no network — so every rule below is unit-tested off-DB
(`tests/test_upload_validation.py`) rather than discovered in production.

The rules, and why each one is here rather than merely nice:

1.  **Allowlist, never blocklist.** Only the seven suffixes `document_loaders` can
    actually parse are accepted. A blocklist of dangerous extensions is a losing game;
    an allowlist bounded by "we have a loader for this" cannot drift.
2.  **The filename is untrusted input.** `../../etc/passwd` and `x\\x00.pdf` are names a
    client can send. `safe_filename` reduces to a basename and a conservative character
    set, so the value that reaches `os.path.join` cannot escape the quarantine directory.
3.  **Content is sniffed, not believed.** Both the extension and the browser's
    `Content-Type` are claims by the uploader. `report.pdf` holding a ZIP is rejected by
    comparing magic bytes against the suffix — which also kills the double-extension
    trick (`x.exe.pdf`), since the last suffix decides the loader and the magic decides
    whether that loader is being lied to.
4.  **Size is capped per type, before anything parses it.** A 2 GB "PDF" must be refused
    at the door, not after PyPDF has been handed it.
5.  **Archives are checked for a decompression bomb.** `.docx` is a ZIP, and a 40 KB ZIP
    that expands to 4 GB is a trivially available denial of service against any endpoint
    that unzips it. The ratio and the uncompressed total are both bounded.
6.  **Text files must be text.** A NUL byte in the first block means the payload is
    binary wearing a `.txt` extension, whatever it claims.

What is deliberately NOT here: VRR-relevance. Whether a document belongs in a reservoir
engineering index is a judgement, and `core/knowledge.py` has always said a human makes
it. These rules decide whether a file is *safe and parseable*, never whether it is
*appropriate* — the review gate decides that, and this module exists to make sure nothing
reaches a reviewer that could hurt the machine on the way.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field

# ------------------------------------------------------------------ policy ----

# Mirrors `pipeline.document_loaders.SUFFIX_LOADERS`. It is duplicated rather than
# imported because `core/` may not import `pipeline/` (see CLAUDE.md), and because the
# two answer different questions: that map says "can we parse it", this one says "will we
# accept it". `test_upload_validation.py` asserts this stays a subset of that.
ALLOWED_SUFFIXES: dict[str, str] = {
    ".pdf": "pdf", ".txt": "text", ".md": "text", ".html": "html", ".htm": "html",
    ".docx": "docx", ".csv": "csv",
}

# Per-kind byte ceilings. A reservoir procedure is prose; anything an order of magnitude
# past these is either a scan nobody will retrieve usefully or a mistake.
MAX_BYTES: dict[str, int] = {
    "pdf": 25 * 1024 * 1024,
    "docx": 15 * 1024 * 1024,
    "csv": 10 * 1024 * 1024,
    "html": 5 * 1024 * 1024,
    "text": 5 * 1024 * 1024,
}
MAX_BYTES_ANY = max(MAX_BYTES.values())
MIN_BYTES = 16                     # below this there is no document, only a mistake

MAX_FILENAME_CHARS = 180
# Kept deliberately tight: letters, digits, space, dot, dash, underscore, parens. Every
# other byte becomes "_", so no shell metacharacter, path separator or control character
# survives into a path, a log line, or the UI.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 ._()\-]")
_DOT_RUN = re.compile(r"\.{2,}")

# Magic-byte prefixes. `None` means "this kind has no signature — verify it is text".
MAGIC: dict[str, tuple[bytes, ...] | None] = {
    "pdf": (b"%PDF-",),
    "docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "text": None,
    "html": None,
    "csv": None,
}

# Signatures we refuse outright whatever the extension claims, because seeing one means
# the extension is a lie. Executables and archives dominate the list for the obvious
# reason; the OLE signature covers legacy .doc/.xls renamed to .docx.
FORBIDDEN_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "an ELF executable"),
    (b"\xca\xfe\xba\xbe", "a Mach-O/Java binary"),
    (b"\xcf\xfa\xed\xfe", "a Mach-O binary"),
    (b"#!", "a shell script"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"BZh", "a bzip2 archive"),
    (b"Rar!", "a RAR archive"),
    (b"7z\xbc\xaf", "a 7-Zip archive"),
    (b"\xd0\xcf\x11\xe0", "a legacy OLE document (.doc/.xls), not .docx"),
)

# Zip-bomb bounds for .docx. A real 15 MB .docx unpacks to well under 200 MB; the ratio
# check catches the small-file case the absolute cap would miss.
MAX_ZIP_RATIO = 120
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ZIP_ENTRIES = 2_000


_MIB = 1024 * 1024


def mb(nbytes: float) -> str:
    """Render a byte count the way the limits are DEFINED — in mebibytes.

    The caps are powers of two, so `nbytes / 1e6` prints a 25 MiB limit as "26 MB": the
    refusal message then disagrees with the constant, with the UI label, and with what a
    file manager shows the uploader. One helper, used everywhere a size is spoken aloud.
    """
    return f"{round(nbytes / _MIB)} MB"


@dataclass(frozen=True)
class Verdict:
    """The outcome. `ok` gates the write; `errors` are shown to the uploader verbatim."""

    ok: bool
    kind: str | None = None                      # pdf | text | html | docx | csv
    safe_name: str = ""
    size_bytes: int = 0
    sha256: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """One line for an HTTP detail body."""
        return "; ".join(self.errors)


# --------------------------------------------------------------- filename ----
def safe_filename(raw: str) -> str:
    """Reduce an uploaded name to something that cannot escape a directory.

    Takes the basename under BOTH separators — a Windows client sends `C:\\docs\\a.pdf`
    and `os.path.basename` on Linux would keep the whole string — then strips the
    character set down and collapses `..` runs. Returns "" when nothing usable is left,
    which the caller must treat as a rejection rather than substituting a default.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "").strip()
    name = _UNSAFE_CHARS.sub("_", name)
    name = _DOT_RUN.sub(".", name).strip(". ")
    if len(name) > MAX_FILENAME_CHARS:           # keep the suffix, truncate the stem
        stem, _, ext = name.rpartition(".")
        keep = MAX_FILENAME_CHARS - len(ext) - 1
        name = f"{stem[:keep]}.{ext}" if ext and keep > 0 else name[:MAX_FILENAME_CHARS]
    return name


def suffix_of(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


# ---------------------------------------------------------------- content ----
# A control character that is not tab/newline/CR/formfeed does not occur in prose. Random
# bytes are ~25% of them (C0 plus the C1 range); real Latin-1 text is ~0%. 10% separates
# the two with enormous margin at any sample size worth reading.
MAX_CONTROL_FRACTION = 0.10
_TEXT_CONTROLS = {0x09, 0x0a, 0x0d, 0x0c, 0x1b}     # tab, LF, CR, FF, ESC-in-ANSI-output


def looks_like_text(data: bytes, sample: int = 8192) -> bool:
    """Is this prose, or a binary wearing a text extension?

    The obvious implementation — "try UTF-8, fall back to Latin-1" — is worthless, and
    it shipped for exactly as long as it took to point 200 bytes of /dev/urandom at the
    endpoint, which came back 201. **Latin-1 cannot fail**: all 256 byte values are
    assigned, so `.decode("latin-1")` succeeds on every input ever, and the only real
    check left was "is there a NUL in here", which random data passes about half the time.

    Latin-1 still has to be ACCEPTED — `document_loaders.load_text` sets
    `autodetect_encoding=True` because field exports genuinely are Latin-1, so refusing
    it would reject files the loader handles fine. So the test is not the codec, it is
    the CHARACTER MIX: text is overwhelmingly printable, and binary is not.
    """
    head = data[:sample]
    if not head:
        return False
    if b"\x00" in head:
        return False
    control = sum(1 for b in head if b < 0x20 and b not in _TEXT_CONTROLS)
    control += sum(1 for b in head if 0x7f <= b <= 0x9f)     # DEL + the C1 block
    return control / len(head) <= MAX_CONTROL_FRACTION


def _forbidden_signature(data: bytes) -> str | None:
    for sig, human in FORBIDDEN_MAGIC:
        if data.startswith(sig):
            return human
    return None


def _check_zip(data: bytes) -> list[str]:
    """Structural checks on a .docx before anything unzips it for real."""
    errs: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a readable .docx (the ZIP container is corrupt)"]
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        errs.append(f"archive has {len(infos):,} entries (limit {MAX_ZIP_ENTRIES:,})")
    total = sum(i.file_size for i in infos)
    if total > MAX_ZIP_UNCOMPRESSED:
        errs.append(f"expands to {mb(total)} (limit {mb(MAX_ZIP_UNCOMPRESSED)})")
    if len(data) and total / len(data) > MAX_ZIP_RATIO:
        errs.append(f"compression ratio {total / len(data):.0f}:1 exceeds "
                    f"{MAX_ZIP_RATIO}:1 — refusing a possible decompression bomb")
    # A .docx always carries this. Its absence means a ZIP was renamed, which is the
    # cheapest way to smuggle arbitrary files past an extension check.
    if not any(i.filename == "[Content_Types].xml" for i in infos):
        errs.append("not a Word document (no [Content_Types].xml in the archive)")
    # Entry names are attacker-controlled and some extractors honour them.
    if any(n.startswith("/") or ".." in n for n in zf.namelist()):
        errs.append("archive contains path-traversal entry names")
    return errs


# --------------------------------------------------------------- the gate ----
def validate_upload(filename: str, data: bytes,
                    declared_type: str | None = None) -> Verdict:
    """The one call the API makes. Collects EVERY failure rather than short-circuiting.

    Returning all of them at once matters for a file picker: telling someone their file
    is too large, and only after they shrink it that the type was never allowed, is two
    round trips for one decision.
    """
    errors: list[str] = []
    warnings: list[str] = []

    name = safe_filename(filename)
    if not name:
        return Verdict(ok=False, errors=["filename is empty or entirely unsafe characters"])
    if name != (filename or "").strip():
        warnings.append(f"stored as {name!r}")

    suffix = suffix_of(name)
    kind = ALLOWED_SUFFIXES.get(suffix)
    if kind is None:
        return Verdict(
            ok=False, safe_name=name, size_bytes=len(data),
            errors=[(f"{suffix or 'no extension'} is not accepted — allowed: "
                     f"{', '.join(sorted(ALLOWED_SUFFIXES))}")])

    size = len(data)
    digest = hashlib.sha256(data).hexdigest()
    limit = MAX_BYTES[kind]
    if size < MIN_BYTES:
        errors.append(f"file is {size} bytes — empty or truncated")
    elif size > limit:
        errors.append(f"{mb(size)} exceeds the {mb(limit)} limit for {kind} files")

    if (human := _forbidden_signature(data)):
        errors.append(f"content is {human}, not a {kind} file")
    else:
        expected = MAGIC[kind]
        if expected and not data.startswith(expected):
            errors.append(f"content does not start with a {kind} signature — the "
                          f"{suffix} extension does not match the bytes")
        elif expected is None and not looks_like_text(data):
            errors.append(f"a {suffix} file must be text; this contains binary data")

    if kind == "docx" and not errors:
        errors += _check_zip(data)

    # The browser's Content-Type is the weakest signal of the three, so a mismatch is a
    # warning for the reviewer and never a rejection on its own — Windows sends
    # `application/octet-stream` for perfectly good PDFs.
    if declared_type and kind == "pdf" and "pdf" not in declared_type.lower():
        warnings.append(f"browser declared {declared_type!r} for a .pdf")

    return Verdict(ok=not errors, kind=kind, safe_name=name, size_bytes=size,
                   sha256=digest, errors=errors, warnings=warnings)
