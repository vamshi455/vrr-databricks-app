"""Loaders, splitters and the retrieval floor — the RAG front half, tested offline.

No network, no Postgres, no Ollama: files are written to tmp_path and the embedder is a
deterministic stub, so these run in the pure `pytest -q` tier with the rest of `core/`.

What they pin: every loader produces `List[Document]` with the metadata a citation needs
(`file_name`, `file_type`, 1-based `page`), a mixed-format folder loads in one pass,
chunk metadata survives splitting, and an unanswerable question ABSTAINS instead of
being handed the nearest four rows.
"""
from __future__ import annotations

import pathlib

import pytest
from langchain_core.documents import Document

from vrr_agent_open.pipeline import document_loaders as DL
from vrr_agent_open.pipeline import text_splitters as TS


# ------------------------------------------------------------------ loaders ----
def test_text_loader_stamps_citation_metadata(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("VRR is injected reservoir volume over produced reservoir volume.")

    docs = DL.load_file(p)

    assert len(docs) == 1
    assert isinstance(docs[0], Document)
    assert docs[0].metadata["file_name"] == "notes.txt"
    assert docs[0].metadata["file_type"] == "text"
    assert "reservoir volume" in docs[0].page_content


def test_csv_loader_yields_one_document_per_row(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("pattern,vrr\nUNITY,1.25\nHORIZON,0.98\n")

    docs = DL.load_file(p)

    assert len(docs) == 2                       # a row is the retrievable unit
    assert "UNITY" in docs[0].page_content
    assert docs[1].metadata["file_type"] == "csv"


def test_html_loader_extracts_text_not_markup(tmp_path):
    p = tmp_path / "guide.html"
    p.write_text("<html><title>VRR</title><body><p>Step limit is 15%.</p></body></html>")

    docs = DL.load_file(p)

    assert "15%" in docs[0].page_content
    assert "<p>" not in docs[0].page_content
    assert docs[0].metadata["file_type"] == "html"


def test_directory_loads_mixed_formats_in_one_pass(tmp_path):
    (tmp_path / "notes.txt").write_text("injection notes")
    (tmp_path / "data.csv").write_text("pattern,vrr\nUNITY,1.25\n")
    (tmp_path / "ignored.bin").write_bytes(b"\x00\x01")     # unsupported: skipped

    docs = DL.load_directory(tmp_path)
    info = DL.summarise(docs)

    assert info["by_type"] == {"text": 1, "csv": 1}
    assert info["files"] == ["data.csv", "notes.txt"]


def test_unknown_suffix_raises_rather_than_guessing(tmp_path):
    p = tmp_path / "seismic.segy"
    p.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="no loader"):
        DL.load_file(p)


def test_pdf_pages_are_one_based_and_citable():
    """PDF loaders count from 0; a citation that says 'p.0' is wrong on its face."""
    pdfs = list(pathlib.Path("knowledge_uploads").glob("*.pdf"))
    if not pdfs:
        pytest.skip("no sample PDFs in knowledge_uploads/")

    docs = DL.load_pdf(pdfs[0])

    assert docs[0].metadata["page"] == 1
    assert docs[0].metadata["file_name"] == pdfs[0].name


# ---------------------------------------------------------------- splitters ----
def fake_embed(text: str) -> list[float]:
    """Deterministic bag-of-characters vector — no model, but similar strings score
    similar, which is all the ranking logic needs to be exercised."""
    vec = [0.0] * 26
    for ch in text.lower():
        if "a" <= ch <= "z":
            vec[ord(ch) - 97] += 1.0
    return vec


def test_fixed_chunking_breaks_sentences_and_recursive_does_not():
    fixed = TS.split_fixed(TS.SAMPLE, size=200)
    recursive = TS.split_recursive(TS.SAMPLE)

    assert TS.boundary_quality(fixed)["ends_on_sentence"] < 0.5
    assert TS.boundary_quality(recursive)["ends_on_sentence"] == 1.0


def test_chunk_metadata_survives_splitting():
    doc = Document(page_content=TS.SAMPLE,
                   metadata={"file_name": "procedure.pdf", "page": 3,
                             "file_type": "pdf"})

    chunks = TS.split_documents([doc], strategy="recursive")

    assert len(chunks) > 1
    for i, c in enumerate(chunks):
        assert c.metadata["file_name"] == "procedure.pdf"   # still citable
        assert c.metadata["page"] == 3
        assert c.metadata["chunk"] == i
        assert c.metadata["chunk_strategy"] == "recursive"


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        TS.split_documents([Document(page_content="x")], strategy="vibes")


def test_semantic_splitter_falls_back_when_the_embedder_is_down():
    """Ingest must not break because Ollama is down — it degrades to recursive."""
    def dead(_text):
        raise RuntimeError("connection refused")

    chunks = TS.split_semantic(TS.SAMPLE, embed=dead)

    assert chunks == TS.split_recursive(TS.SAMPLE)


def test_retrieval_check_scores_recall_and_mrr():
    """The chunking test itself, computable without a live model."""
    scored = TS.retrieval_check(TS.split_recursive(TS.SAMPLE), k=2, embed=fake_embed)

    assert scored["ok"] is True
    assert 0.0 <= scored["recall@2"] <= 1.0
    assert 0.0 <= scored["mrr"] <= 1.0
    assert len(scored["detail"]) == len(TS.PROBES)


def test_retrieval_check_reports_a_missing_needle_as_unranked():
    """A probe whose phrase is in no chunk must show rank None, not a silent pass."""
    scored = TS.retrieval_check(["nothing relevant here"],
                                probes=[("what is the step limit?", "15%")],
                                embed=fake_embed)

    assert scored["detail"][0]["rank"] is None
    assert scored["recall@2"] == 0.0


# ------------------------------------------------- the abstain path (floor) ----
def test_empty_retrieval_abstains_instead_of_answering(monkeypatch):
    """The point of the similarity floor: nothing cleared it → say "I don't know" and
    never call the model. A confident answer over irrelevant excerpts is the failure
    this prevents."""
    from vrr_agent_open.agent import chat as CH

    monkeypatch.setattr(CH.T, "search_knowledge",
                        lambda q, k=3: {"ok": True, "hits": [], "min_score": 0.35})
    called = []
    monkeypatch.setattr(CH.llm, "chat", lambda *a, **k: called.append(1) or {})

    out = CH._knowledge_answer("what is the flare gas recovery spec?")

    assert "don't know" in out["text"].lower()
    assert out["meta"]["retrieved"] == 0
    assert "abstained" in out["meta"]["gate"]
    assert called == []                        # the model was never asked


def test_retrieval_floor_is_configurable_and_defaults_conservative():
    from vrr_agent_open.config import load_config

    assert 0.0 < load_config().retrieval_min_score < 1.0
