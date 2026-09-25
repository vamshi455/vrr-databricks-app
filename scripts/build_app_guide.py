"""Generate the application user guide, then ingest it as the `app_help` corpus.

    make guide

**One source of truth.** The guide is generated from `core/help_topics.py`, not written
alongside it. Two hand-maintained descriptions of one application is how a user guide ends
up confidently describing a button that was renamed six months ago — and a stale guide in
a vector index is worse than no guide, because the agent quotes it with a citation.

**Why these documents skip the human approval gate, and why that is not a hole.** The gate
in `api/routes_knowledge.py` exists because an *uploaded* document is content of unknown
provenance and unknown relevance, and a person must judge it. These files have neither
problem: they are generated from this repository's own source by an operator running a
make target, exactly like the seeded demo PDFs. The judgement was made when the topic was
written. Nothing reaching this script came from a browser.

They are ingested as `doc_kind='app_help'`, a corpus that `search()` never mixes with the
reservoir documents — see the note on that function.
"""
from __future__ import annotations

import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from vrr_agent_open.core import help_topics as HELP
from vrr_agent_open.pipeline import knowledge_ingest as KI

GUIDE_DIR = pathlib.Path("docs/app-guide")
DOC_KIND = "app_help"

# One file per view, so a citation reads "04-approvals.md p.1" and names the screen the
# reader is looking at rather than a generic "user_guide.pdf".
SLUG = {
    "": "00-general", "Portfolio": "01-portfolio", "Report": "02-report",
    "Lineage & audit": "03-lineage-audit", "Approvals": "04-approvals",
    "Knowledge": "05-knowledge",
}
HEADER = ("_Generated from `core/help_topics.py` by `make guide`. Edit that file and "
          "re-run; do not edit this document by hand — it is overwritten._")


def generate() -> list[pathlib.Path]:
    GUIDE_DIR.mkdir(parents=True, exist_ok=True)
    by_view: dict[str, list] = {}
    for t in HELP.TOPICS:
        by_view.setdefault(t.view, []).append(t)

    written = []
    for view, topics in by_view.items():
        path = GUIDE_DIR / f"{SLUG[view]}.md"
        lines = [f"# Meridian Petroleum workbench — {view or 'General'}", "", HEADER, ""]
        for t in topics:
            lines += [f"## {t.title}", "", t.body, ""]
        path.write_text("\n".join(lines))
        written.append(path)
    return sorted(written)


def ingest(paths: list[pathlib.Path]) -> dict:
    """Copy into the knowledge volume, register as approved `app_help`, embed.

    Registered with the CONTENT hash as the doc_id rather than the filename hash the
    folder flow uses, so editing a topic and re-running actually re-embeds instead of
    silently keeping the old chunks under an unchanged id.
    """
    import hashlib

    upload_dir = pathlib.Path(KI.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    total_chunks = 0
    done = []
    with KI._conn() as c, c.cursor() as cur:
        for src in paths:
            data = src.read_bytes()
            doc_id = "guide-" + hashlib.sha256(data).hexdigest()[:20]
            dest = upload_dir / src.name
            shutil.copyfile(src, dest)

            # Drop any earlier revision of this same guide file, so re-running does not
            # leave last week's wording in the index competing with this week's.
            cur.execute("DELETE FROM vrr_agent.reservoir_knowledge"
                        " WHERE file_name=%s AND doc_kind=%s", (src.name, DOC_KIND))
            cur.execute("DELETE FROM vrr_agent.knowledge_registry"
                        " WHERE file_name=%s AND doc_kind=%s", (src.name, DOC_KIND))
            cur.execute(
                "INSERT INTO vrr_agent.knowledge_registry"
                " (doc_id, file_name, status, source, doc_kind, reviewed_by, reviewed_at,"
                "  content_kind, size_bytes)"
                " VALUES (%s,%s,'approved','generated',%s,'make guide',now(),'text',%s)"
                " ON CONFLICT (doc_id) DO UPDATE SET status='approved'",
                (doc_id, src.name, DOC_KIND, len(data)))
            out = KI._embed_one(cur, doc_id, src.name, "recursive", DOC_KIND)
            total_chunks += out["n_chunks"]
            done.append((src.name, out["n_chunks"]))
        c.commit()
    return {"files": done, "chunks": total_chunks}


def main() -> None:
    paths = generate()
    print(f"generated {len(paths)} guide file(s) from {len(HELP.TOPICS)} topics:")
    for p in paths:
        print(f"  {p}")
    try:
        res = ingest(paths)
    except Exception as exc:
        print(f"\n! not ingested: {exc}")
        print("  The written answers still work — they need no database and no model.")
        print("  Ingestion only adds the long-tail fallback; start Postgres and Ollama,")
        print("  then re-run `make guide`.")
        return
    print(f"\ningested as doc_kind='{DOC_KIND}' — {res['chunks']} chunks:")
    for name, n in res["files"]:
        print(f"  {name:22} {n} chunk(s)")
    print("\nAsk the agent: \"how do I move a card from analyst to RM?\"")


if __name__ == "__main__":
    main()
