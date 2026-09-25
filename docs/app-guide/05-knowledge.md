# Meridian Petroleum workbench — Knowledge

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## The Knowledge view — uploading documents

**Knowledge — what the agent is allowed to read.**

Uploading is **not** the same as indexing. A file you upload lands in quarantine as
`pending_review` and answers nothing. A `data_steward` or `admin` then opens it, reads
the text that was *actually extracted* — which is not what the document looks like in a
viewer — and approves it. Approving embeds it in seconds, and it is askable in the chat
immediately after.

That gate is deliberate: whether a document belongs in a reservoir index is a judgement,
and no model makes it.

Accepted: `.pdf` 25 MB · `.docx` 15 MB · `.csv` 10 MB · `.html`/`.htm`, `.md`/`.txt` 5 MB.

**If your file was refused**, the message says exactly why. The usual causes are an
extension outside that list, contents that do not match the extension (a renamed file),
or identical content already in the index. Any PII found — emails, phones, credentials —
is replaced with `[REDACTED:kind]` before embedding, so the raw value never reaches the
database.

"Remove from index" drops the chunks and keeps the registry row: what was ingested, by
whom, and whether it held PII stays on the record.
