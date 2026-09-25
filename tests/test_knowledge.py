"""Unit tests for knowledge chunking + PII detection/redaction — off-cluster."""

from vrr_agent_open.core import knowledge as K


# ---- PII detection ---------------------------------------------------------

def test_detects_email_phone_ssn():
    text = ("Contact john.doe@acme.com or (555) 123-4567. "
            "Badge SSN 123-45-6789 on file.")
    kinds = {p["kind"] for p in K.detect_pii(text)}
    assert {"email", "phone", "ssn"} <= kinds


def test_detects_credentials_and_cards():
    hits = K.detect_pii("api_key: sk-abc123 and card 4111 1111 1111 1111")
    kinds = {p["kind"] for p in hits}
    assert "credential" in kinds and "credit_card" in kinds


def test_clean_engineering_text_has_no_pii():
    text = ("Pattern UNITY VRR was 1.31 in March; injection pressure 2450 psi, "
            "fracture gradient 3100 psi. Reduce water injection by 10%.")
    assert K.detect_pii(text) == []


def test_redaction_removes_pii_and_reports_kinds():
    clean, kinds = K.redact_pii("Email a@b.com, SSN 123-45-6789.")
    assert "a@b.com" not in clean and "123-45-6789" not in clean
    assert "[REDACTED:email]" in clean and "[REDACTED:ssn]" in clean
    assert sorted(kinds) == ["email", "ssn"]


def test_redaction_is_noop_on_clean_text():
    clean, kinds = K.redact_pii("VRR = INJ_RES / PROD_RES, target 1.0.")
    assert kinds == [] and "REDACTED" not in clean


# ---- chunking --------------------------------------------------------------

def test_short_text_is_one_chunk():
    assert K.chunk_text("one small paragraph") == ["one small paragraph"]


def test_paragraphs_pack_up_to_budget():
    paras = [f"para {i} " + "x" * 400 for i in range(6)]
    chunks = K.chunk_text("\n\n".join(paras), max_chars=1000, overlap=50)
    assert len(chunks) >= 3
    # every paragraph's label survives somewhere
    joined = " ".join(chunks)
    assert all(f"para {i}" in joined for i in range(6))


def test_oversized_paragraph_is_hard_split_with_overlap():
    words = " ".join(f"w{i:04d}" for i in range(600))   # ~4200 chars, no \n\n
    chunks = K.chunk_text(words, max_chars=1000, overlap=100)
    assert all(len(c) <= 1100 for c in chunks)          # budget + overlap slack
    # overlap: the start of chunk 2 repeats the tail of chunk 1
    tail_word = chunks[0].split()[-1]
    assert tail_word in chunks[1]
    # nothing lost
    for i in (0, 299, 599):
        assert f"w{i:04d}" in " ".join(chunks)


def test_empty_and_whitespace_input():
    assert K.chunk_text("") == []
    assert K.chunk_text("\n\n  \n\n") == []
