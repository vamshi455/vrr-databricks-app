"""Knowledge upload + review — the folder-drop flow, made safe to expose to a browser.

    POST   /api/knowledge/upload                      data_steward|admin  → pending_review
    GET    /api/knowledge/documents                   the review queue + what is live
    GET    /api/knowledge/documents/{id}/preview      extracted text + PII, embeds nothing
    POST   /api/knowledge/documents/{id}/approve      → embed now → askable in chat
    POST   /api/knowledge/documents/{id}/reject       with a reason, kept on the row
    DELETE /api/knowledge/documents/{id}              chunks out of the index

**The gate is preserved, not removed.** `core/knowledge.py` has always said the
VRR-relevance of a document is a human judgement and is deliberately not automated. An
upload button that embedded on arrival would delete that rule to make a spinner shorter.
So an upload lands in quarantine as `pending_review` and is not searchable; a
`data_steward` reads the real extracted text, sees the PII findings, and approves — and
*then* it embeds, in seconds, in the same request. Instant for the reviewer; still a
decision, still attributed (`reviewed_by` and `reviewed_at` come from the token).

**Validation is layered, and every layer refuses on its own.** The browser's `accept`
attribute is a convenience for the file picker and nothing more — it is trivially
bypassed by any HTTP client, so it is not counted as a control:

    1. role          data_steward|admin, from the signed claim (api/auth.py)
    2. rate          per-user budget (api/ratelimit.py) — auth is not a quota
    3. size          streamed and aborted mid-upload; never buffered whole first
    4. bytes         core/upload_validation.py — allowlist, magic, zip bombs, traversal
    5. corpus quota  a bounded index, so one uploader cannot crowd out every other doc
    6. dedupe        content hash, so the same bytes cannot be embedded twice
    7. HUMAN         status stays pending_review until a person approves it

Only layers 1-6 are here. Layer 7 is the point of the whole file.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from ..core import upload_validation as UV
from . import ratelimit as RL
from .auth import CurrentUser, require_role
from .db import execute, query

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# The same directory `make knowledge` walks, so an uploaded file and a dropped file are
# the same thing to every downstream stage. Resolved once, absolute, and every write is
# asserted to stay inside it.
UPLOAD_DIR = pathlib.Path(os.environ.get("VRR_KNOWLEDGE_DIR", "./knowledge_uploads")).resolve()

# Corpus ceilings. A vector index is a shared, ranked resource: the top-k a question gets
# back is finite, so an unbounded corpus does not merely cost disk — it dilutes retrieval
# for every other document. Bounded here, tunable by env for a real deployment.
MAX_DOCUMENTS = int(os.environ.get("VRR_KNOWLEDGE_MAX_DOCS", "200"))
MAX_CHUNKS = int(os.environ.get("VRR_KNOWLEDGE_MAX_CHUNKS", "20000"))

# Streaming read granularity. Chosen so the abort on an oversized upload happens after
# ~64 KB rather than after the client has finished sending gigabytes.
_READ_CHUNK = 64 * 1024

# Declared as Annotated aliases, NOT as `user: CurrentUser = require_role(...)`. That
# spelling silently does nothing: an `Annotated[..., Depends(...)]` annotation wins over
# the parameter's default, so FastAPI resolved `current_user` and the role check never
# ran — every authenticated caller could upload. Caught by `test_non_steward_roles_
# cannot_upload`, which is why the role tests enumerate all three non-steward roles
# instead of spot-checking one.
Uploader = Annotated[dict, Depends(require_role("data_steward", "admin"))]
Reviewer = Annotated[dict, Depends(require_role("data_steward", "admin"))]


# ------------------------------------------------------------------ helpers ----
def _resolve_stored(name: str) -> pathlib.Path:
    """Join a validated name to the quarantine dir and PROVE the result stays inside it.

    `safe_filename` already stripped separators, so this is the belt to that braces. It
    is here because the consequence of being wrong — a write anywhere on the filesystem
    the API process can reach — is severe enough to be worth checking twice, and because
    the check is one line.
    """
    target = (UPLOAD_DIR / name).resolve()
    if target.parent != UPLOAD_DIR:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "refusing a path outside the upload directory")
    return target


def _corpus_usage() -> dict:
    rows = query("SELECT (SELECT count(*) FROM vrr_agent.knowledge_registry"
                 "         WHERE status <> 'rejected') AS docs,"
                 "       (SELECT count(*) FROM vrr_agent.reservoir_knowledge) AS chunks")
    r = rows[0] if rows else {"docs": 0, "chunks": 0}
    return {"docs": r["docs"], "chunks": r["chunks"],
            "max_docs": MAX_DOCUMENTS, "max_chunks": MAX_CHUNKS}


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Read the body, refusing past `cap` WITHOUT buffering the whole thing first.

    `await upload.read()` with no argument reads everything, which hands an attacker a
    memory-exhaustion primitive that costs them one request. The Content-Length header is
    checked first as a cheap early exit, but it is a client-supplied number and is never
    trusted as the actual limit — the loop below is what enforces it.
    """
    buf = bytearray()
    while chunk := await upload.read(_READ_CHUNK):
        buf += chunk
        if len(buf) > cap:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"upload exceeds the hard limit of {UV.mb(cap)}")
    return bytes(buf)


def _doc_or_404(doc_id: str) -> dict:
    rows = query("SELECT * FROM vrr_agent.knowledge_registry WHERE doc_id = %(d)s",
                 {"d": doc_id})
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no document {doc_id}")
    return rows[0]


# ------------------------------------------------------------------- upload ----
@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(user: Uploader, file: UploadFile = File(...)) -> dict:   # noqa: B008
    """Accept a document into QUARANTINE. It is not searchable until someone approves it.

    Returns 201 with `status: "pending_review"` — deliberately not 200, because nothing
    was made available; a resource was created awaiting a decision.
    """
    RL.hit("upload", user["username"])

    usage = _corpus_usage()
    if usage["docs"] >= MAX_DOCUMENTS:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"knowledge base is full ({usage['docs']}/{MAX_DOCUMENTS} "
                            "documents) — remove one before adding another")
    if usage["chunks"] >= MAX_CHUNKS:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"chunk budget exhausted ({usage['chunks']:,}/{MAX_CHUNKS:,})")

    data = await _read_capped(file, UV.MAX_BYTES_ANY)
    verdict = UV.validate_upload(file.filename or "", data, file.content_type)
    if not verdict.ok:
        # 422, not 400: the request was well-formed HTTP; its CONTENT was refused. The
        # errors go back verbatim so the uploader can fix the file rather than guess.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"rejected": verdict.errors, "file": verdict.safe_name})

    # Content-addressed dedupe BEFORE touching the disk. Re-uploading the same bytes under
    # a new name would otherwise put a second copy of every chunk in the index, where the
    # duplicates compete with other documents for the same finite top-k.
    dup = query("SELECT doc_id, file_name, status FROM vrr_agent.knowledge_registry"
                " WHERE sha256 = %(h)s", {"h": verdict.sha256})
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"identical content already registered as "
                            f"{dup[0]['file_name']!r} ({dup[0]['status']})")

    # The doc_id is the content hash, not the filename. The folder flow hashes the NAME
    # (`sha1(f)`), which means editing a file in place and re-registering it silently
    # keeps the old id and the old chunks. Distinct bytes get a distinct id here.
    doc_id = f"up-{verdict.sha256[:24]}"
    stored = verdict.safe_name
    target = _resolve_stored(stored)
    if target.exists():                       # same name, different bytes — keep both
        target = _resolve_stored(f"{target.stem}-{verdict.sha256[:8]}{target.suffix}")
        stored = target.name

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        execute(
            "INSERT INTO vrr_agent.knowledge_registry"
            " (doc_id, file_name, status, source, uploaded_by, stored_name,"
            "  content_kind, size_bytes, sha256)"
            " VALUES (%(d)s, %(f)s, 'pending_review', 'upload', %(u)s, %(s)s,"
            "         %(k)s, %(z)s, %(h)s)",
            {"d": doc_id, "f": stored, "u": user["username"], "s": stored,
             "k": verdict.kind, "z": verdict.size_bytes, "h": verdict.sha256})
    except Exception:
        target.unlink(missing_ok=True)        # no orphan file for a row that never landed
        raise

    return {"doc_id": doc_id, "file_name": stored, "status": "pending_review",
            "kind": verdict.kind, "size_bytes": verdict.size_bytes,
            "sha256": verdict.sha256, "warnings": verdict.warnings,
            "uploaded_by": user["username"],
            "next": "a data steward must approve it before it can be searched"}


# -------------------------------------------------------------------- list ----
@router.get("/documents")
def documents(user: CurrentUser, status_filter: str | None = Query(None, alias="status"),
              limit: int = Query(100, ge=1, le=500)) -> dict:
    """The review queue and the live corpus in one call.

    Authenticated even though it only reads: the registry carries uploader identities and
    which documents contained credentials, which is not portfolio data and has no reason
    to be world-readable the way a VRR trend does.
    """
    if status_filter and status_filter not in {"pending_review", "approved", "rejected"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "status must be pending_review, approved or rejected")
    rows = query(
        # doc_kind travels to the UI so the six generated user-guide files do not read as
        # documents somebody uploaded and forgot to review — they are a different corpus,
        # `make guide` owns them, and a steward should not be invited to curate them.
        "SELECT doc_id, file_name, status, source, uploaded_by, reviewed_by, reviewed_at,"
        "       review_note, ingest_error, content_kind, size_bytes, n_chunks,"
        "       coalesce(doc_kind, 'reservoir') AS doc_kind,"
        "       pii_found, pii_kinds, registered_at"
        " FROM vrr_agent.knowledge_registry"
        # ::text is required, not decoration. `$1 IS NULL` gives Postgres nothing to infer
        # the parameter's type from, and it answers AmbiguousParameter — a 500 on the
        # first load of the view. The unit tests stub `query`, so no amount of them would
        # have caught it; the rendered page did, on the first screenshot.
        " WHERE (%(s)s::text IS NULL OR status = %(s)s::text)"
        " ORDER BY registered_at DESC LIMIT %(l)s",
        {"s": status_filter, "l": limit})
    return {"documents": rows, "usage": _corpus_usage(),
            "can_review": user["role"] in {"data_steward", "admin"},
            "accepted_types": sorted(UV.ALLOWED_SUFFIXES),
            "max_bytes": UV.MAX_BYTES}


# ----------------------------------------------------------------- preview ----
@router.get("/documents/{doc_id}/preview")
def preview(doc_id: str, user: Reviewer) -> dict:
    """Extracted text + PII findings + chunk count. Embeds nothing, calls no model.

    This is what makes the approval a real decision instead of a rubber stamp: the
    reviewer reads what would actually be indexed, which is not the same as what the
    document looks like in a PDF viewer. The text is PII-redacted before it leaves the
    server — the reviewer needs to know a credential is present, not to read it.
    """
    doc = _doc_or_404(doc_id)
    from ..pipeline import knowledge_ingest as KI  # lazy: pulls loaders + splitters

    try:
        out = KI.preview_document(doc["file_name"])
    except FileNotFoundError:
        raise HTTPException(status.HTTP_410_GONE,
                            "the file is registered but no longer on disk") from None
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"could not parse this document: {exc}") from exc
    return {**out, "doc_id": doc_id, "status": doc["status"],
            "uploaded_by": doc.get("uploaded_by")}


# ----------------------------------------------------------- approve/reject ----
@router.post("/documents/{doc_id}/approve")
def approve(doc_id: str, user: Reviewer) -> dict:
    """The human gate. Approve → embed in this request → immediately askable in chat.

    `reviewed_by` comes from the token, never the body — the same rule the approval chain
    learned the hard way, and for the same reason: a client-supplied approver on an audit
    record is a signature anyone can forge.

    Idempotent: approving an already-ingested document returns its chunk count and
    re-embeds nothing.
    """
    RL.hit("review", user["username"])
    doc = _doc_or_404(doc_id)
    if doc["status"] == "approved" and doc["n_chunks"]:
        return {"doc_id": doc_id, "status": "approved", "n_chunks": doc["n_chunks"],
                "already": True}

    usage = _corpus_usage()
    if usage["chunks"] >= MAX_CHUNKS:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"chunk budget exhausted ({usage['chunks']:,}/{MAX_CHUNKS:,})")

    execute("UPDATE vrr_agent.knowledge_registry SET status='approved', reviewed_by=%(u)s,"
            " reviewed_at=now(), review_note=NULL, ingest_error=NULL WHERE doc_id=%(d)s",
            {"u": user["username"], "d": doc_id})

    from ..pipeline import knowledge_ingest as KI

    try:
        out = KI.ingest_document(doc_id, doc["file_name"])
    except Exception as exc:
        # Roll the decision back. Leaving it 'approved' with no chunks would show a
        # reviewer a document they believe is searchable and is not — and `make knowledge`
        # would then retry it forever, since its sweep selects exactly that state.
        execute("UPDATE vrr_agent.knowledge_registry SET status='pending_review',"
                " reviewed_by=NULL, reviewed_at=NULL, ingest_error=%(e)s WHERE doc_id=%(d)s",
                {"e": str(exc)[:500], "d": doc_id})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"embedding failed, document left pending: {exc}") from exc

    return {**out, "status": "approved", "reviewed_by": user["username"],
            "searchable": out["n_chunks"] > 0,
            "note": ("indexed — ask about it in the chat drawer" if out["n_chunks"]
                     else "approved but produced no text: nothing was indexed")}


@router.post("/documents/{doc_id}/reject")
def reject(doc_id: str, user: Reviewer,
           note: str = Query("", max_length=500)) -> dict:
    """Refuse a document, with the reason recorded on the row.

    The file stays on disk and the row stays in the registry. "Rejected, by whom, and
    why" is the part of a review worth keeping — a queue that forgets its refusals
    invites the same document to be uploaded again next week.
    """
    RL.hit("review", user["username"])
    _doc_or_404(doc_id)
    execute("UPDATE vrr_agent.knowledge_registry SET status='rejected', reviewed_by=%(u)s,"
            " reviewed_at=now(), review_note=%(n)s WHERE doc_id=%(d)s",
            {"u": user["username"], "n": note or None, "d": doc_id})
    return {"doc_id": doc_id, "status": "rejected", "reviewed_by": user["username"],
            "note": note or None}


@router.delete("/documents/{doc_id}")
def remove(doc_id: str, user: Reviewer) -> dict:
    """Take a document OUT of the vector index. The registry row survives.

    Deleting the chunks stops it answering questions; deleting the record of what was
    once ingested, by whom, and whether it held PII would erase the audit trail this
    pipeline exists to keep. Same reasoning as `POST /chat/clear`, which hides a
    transcript and deletes nothing.
    """
    RL.hit("review", user["username"])
    _doc_or_404(doc_id)
    from ..pipeline import knowledge_ingest as KI

    n = KI.delete_document(doc_id)
    execute("UPDATE vrr_agent.knowledge_registry SET status='rejected', reviewed_by=%(u)s,"
            " reviewed_at=now(), review_note='withdrawn from the index' WHERE doc_id=%(d)s",
            {"u": user["username"], "d": doc_id})
    return {"doc_id": doc_id, "chunks_removed": n, "status": "rejected",
            "note": "registry row retained as the audit record"}


# Re-exported so `make knowledge`'s folder flow and the API agree on one hash function
# for names, rather than each inventing its own.
def name_doc_id(file_name: str) -> str:
    return hashlib.sha1(file_name.encode()).hexdigest()
