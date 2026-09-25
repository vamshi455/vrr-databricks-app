"""Raw files → `List[Document]` — the front half of the knowledge path.

Every loader here returns LangChain `Document`s (`page_content` + `metadata`), so the
rest of the pipeline never asks what a source was: `text_splitters.py` chunks them, and
`knowledge_ingest.py` PII-redacts, embeds and stores them in pgvector.

    docs/                                   ┌─ report.pdf   → 12 Documents (1 per page)
      report.pdf   notes.txt   data.csv  ───┤  notes.txt    →  1 Document
      guide.pdf    readme.txt  summary.pdf  └─ data.csv     →  1 Document per row
                                               ↓
                                        List[Document]
                                     page_content + metadata
                                    {source, file_name, file_type, page, …}

Format → loader:

    .pdf   PyPDFLoader        one Document per page, `page` in metadata
           PyMuPDFLoader      same shape, faster on bulk — `backend="pymupdf"`
    .txt   TextLoader         one Document, whole file
    .md    TextLoader         same (a splitter, not a loader, understands headings)
    .html  BSHTMLLoader       BeautifulSoup text extraction, keeps <title>
    .docx  Docx2txtLoader     one Document, whole file
    .csv   CSVLoader          one Document PER ROW
    dir    per-suffix walk    mixed formats in one pass
    http   WebBaseLoader      one Document per URL

Anything messier than these (scanned PDFs, mixed tables/figures, PowerPoint) wants
`UnstructuredFileLoader`, which is an optional extra because it drags in a large
dependency tree — see `load_unstructured` at the bottom.

Run it: `make loaders` · `make loaders from=./docs` · `make loaders from=https://…`
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

# Every VRR document lands in the index with these keys, whatever it was loaded from —
# `file_name` and `page` are what a RAG answer cites, so they are not optional.
BASE_METADATA = ("source", "file_name", "file_type")

SUFFIX_LOADERS = {
    ".pdf": "pdf", ".txt": "text", ".md": "text", ".html": "html", ".htm": "html",
    ".docx": "docx", ".csv": "csv",
}


def _stamp(docs: Iterable[Document], path: Path, file_type: str) -> list[Document]:
    """Normalise metadata across loaders.

    Each loader writes its own dialect (`source` only, or `page` 0-based, or nothing at
    all). Downstream code and the citation line in a RAG answer need one shape, so it is
    imposed here rather than special-cased at every read site.
    """
    out = []
    for d in docs:
        d.metadata.setdefault("source", str(path))
        d.metadata["file_name"] = path.name
        d.metadata["file_type"] = file_type
        if "page" in d.metadata:                 # PDF loaders are 0-based; humans are not
            d.metadata["page"] = int(d.metadata["page"]) + 1
        out.append(d)
    return out


# --------------------------------------------------------------- per format ----
def load_pdf(path: str | Path, backend: str = "pypdf") -> list[Document]:
    """One Document per page.

    `pypdf` is pure Python and is what `make knowledge` uses. `pymupdf` is a C library:
    materially faster and better at multi-column layouts, so it is the one to use for a
    bulk backfill — same Document shape either way, so nothing downstream changes.
    """
    path = Path(path)
    if backend == "pymupdf":
        from langchain_community.document_loaders import PyMuPDFLoader as Loader
    else:
        from langchain_community.document_loaders import PyPDFLoader as Loader
    return _stamp(Loader(str(path)).load(), path, "pdf")


def load_text(path: str | Path) -> list[Document]:
    """One Document for the whole file. `autodetect_encoding` because field exports are
    routinely latin-1, and a UnicodeDecodeError mid-batch is a bad way to find out."""
    from langchain_community.document_loaders import TextLoader

    path = Path(path)
    return _stamp(TextLoader(str(path), autodetect_encoding=True).load(), path, "text")


def load_html(path: str | Path) -> list[Document]:
    """BeautifulSoup text extraction — markup out, `<title>` kept in metadata.

    Pinned to the stdlib `html.parser` so the install stays small: BSHTMLLoader defaults
    to `lxml`, which is a C extension nothing else here needs.
    """
    from langchain_community.document_loaders import BSHTMLLoader

    path = Path(path)
    return _stamp(BSHTMLLoader(str(path), bs_kwargs={"features": "html.parser"}).load(),
                  path, "html")


def load_docx(path: str | Path) -> list[Document]:
    from langchain_community.document_loaders import Docx2txtLoader

    path = Path(path)
    return _stamp(Docx2txtLoader(str(path)).load(), path, "docx")


def load_csv(path: str | Path) -> list[Document]:
    """One Document PER ROW — each row becomes `column: value` lines.

    Right for a lookup table (one row per completion, retrieved individually); wrong for
    a 200k-row export, which belongs in a Postgres table the tools can query, not in a
    vector index. The knowledge index is for prose an engineer would read.
    """
    from langchain_community.document_loaders import CSVLoader

    path = Path(path)
    return _stamp(CSVLoader(str(path)).load(), path, "csv")


def load_file(path: str | Path) -> list[Document]:
    """Dispatch one file by suffix. Unknown suffixes raise rather than guess."""
    path = Path(path)
    kind = SUFFIX_LOADERS.get(path.suffix.lower())
    if kind is None:
        raise ValueError(f"no loader for {path.suffix!r} "
                         f"(have {', '.join(sorted(SUFFIX_LOADERS))})")
    return {"pdf": load_pdf, "text": load_text, "html": load_html,
            "docx": load_docx, "csv": load_csv}[kind](path)


# ------------------------------------------------------------ dir + the web ----
def load_directory(folder: str | Path, recursive: bool = True) -> list[Document]:
    """Walk a folder and load every supported file, mixed formats and all.

    Hand-rolled over `DirectoryLoader(loader_cls=…)` for one reason: DirectoryLoader
    takes a SINGLE loader class, so a folder of .pdf + .txt + .csv needs one pass per
    glob. This dispatches per file, and a file that fails to parse is reported and
    skipped instead of killing the batch.
    """
    folder = Path(folder)
    paths = sorted(p for p in (folder.rglob("*") if recursive else folder.glob("*"))
                   if p.is_file() and p.suffix.lower() in SUFFIX_LOADERS)
    docs: list[Document] = []
    for p in paths:
        try:
            docs += load_file(p)
        except Exception as e:                   # one bad file must not lose the batch
            print(f"  ! skipped {p.name}: {e}")
    return docs


def load_urls(urls: str | list[str]) -> list[Document]:
    """One Document per URL. WebBaseLoader takes a list, so multiple pages are one call
    (and one session) rather than a loop."""
    from langchain_community.document_loaders import WebBaseLoader

    if isinstance(urls, str):
        urls = [urls]
    docs = WebBaseLoader(urls).load()
    for d in docs:
        d.metadata.setdefault("source", "")
        d.metadata["file_name"] = d.metadata.get("title") or d.metadata["source"]
        d.metadata["file_type"] = "web"
    return docs


def load_unstructured(path: str | Path, mode: str = "elements") -> list[Document]:
    """Complex/mixed documents (scanned PDFs, tables, slides) via `unstructured`.

    Optional: `pip install -e ".[complexdocs]"` pulls a large dependency tree (and OCR
    binaries for scans), which is why it is not in the default install. `mode="elements"`
    keeps titles/tables/lists as separate Documents, which chunks far better than one
    flattened blob.
    """
    try:
        from langchain_community.document_loaders import UnstructuredFileLoader
    except ImportError as e:                     # pragma: no cover - optional extra
        raise ImportError('unstructured is an optional extra: '
                          'pip install -e ".[complexdocs]"') from e
    path = Path(path)
    return _stamp(UnstructuredFileLoader(str(path), mode=mode).load(), path,
                  path.suffix.lstrip(".").lower() or "unknown")


def summarise(docs: list[Document]) -> dict[str, Any]:
    """What a batch actually produced — the check to run before embedding anything."""
    by_type: dict[str, int] = {}
    for d in docs:
        key = d.metadata.get("file_type", "?")
        by_type[key] = by_type.get(key, 0) + 1
    chars = sum(len(d.page_content) for d in docs)
    return {"documents": len(docs), "by_type": by_type, "total_chars": chars,
            "avg_chars": round(chars / len(docs)) if docs else 0,
            "files": sorted({d.metadata.get("file_name", "?") for d in docs})}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "./knowledge_uploads"
    loaded = (load_urls(target) if target.startswith("http")
              else load_directory(target) if Path(target).is_dir()
              else load_file(target))
    info = summarise(loaded)
    print(f"{info['documents']} Document(s) from {len(info['files'])} file(s): "
          f"{info['by_type']}")
    print(f"{info['total_chars']:,} chars, {info['avg_chars']:,} avg per Document\n")
    for d in loaded[:3]:
        print(f"  {d.metadata.get('file_name')} "
              f"p.{d.metadata.get('page', '-')} | {len(d.page_content)} chars")
        print(f"    metadata: {d.metadata}")
        print(f"    {d.page_content[:160].strip()}…\n")
