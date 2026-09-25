/**
 * Knowledge — upload a document, review what it actually extracts, approve it into the
 * vector index, then ask about it in the chat drawer.
 *
 * The shape of this screen is an argument about trust, not a layout preference.
 *
 * - **Upload does not mean indexed.** A file lands in quarantine as `pending_review` and
 *   answers nothing. The review step is the guardrail `core/knowledge.py` has always
 *   described ("only VRR-related documents may be embedded"), moved out of a psql UPDATE
 *   and into a place where a person can actually exercise it.
 * - **The reviewer reads the EXTRACTION, not the document.** What gets embedded is the
 *   text a loader pulled out, which is not what a PDF looks like in a viewer — a scan
 *   with no text layer looks perfect and extracts to nothing. So the preview shows the
 *   real extracted characters, the chunk count, and the PII the scanner found.
 * - **Client-side checks are a courtesy, and say so.** The size and type checks here
 *   exist to fail in 5ms instead of after a 25 MB upload. `core/upload_validation.py` is
 *   the control; this file cannot be, because any HTTP client skips it entirely.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, api,
  type ApproveResult, type KnowledgeDoc, type KnowledgeList, type KnowledgePreview,
} from "../api";
import { Badge, Banner, Card, ErrorNote, Metric, Spinner, StatusIcon } from "../components/ui";

interface Props {
  role: string;
  signedIn: boolean;
  onNeedSignIn: () => void;
  /** Bumped after an approval so the shell can refresh the health badge counts. */
  onChanged?: () => void;
}

/** Mirrors `core/upload_validation.MAX_BYTES`. Duplicated knowingly: the server value is
 *  authoritative and arrives in the list payload, this is the pre-flight default before
 *  that first request lands. */
const FALLBACK_MAX = 25 * 1024 * 1024;
const REVIEW_ROLES = ["data_steward", "admin"];

/** Which suffixes map to which server-side kind — mirrors `ALLOWED_SUFFIXES`. Needed so
 *  the pre-flight uses the SAME per-kind ceiling the server will, rather than the largest
 *  one: a 20 MB .txt passes a 25 MB check here and is then refused at 5 MB. */
const SUFFIX_KIND: Record<string, string> = {
  ".pdf": "pdf", ".txt": "text", ".md": "text", ".html": "html", ".htm": "html",
  ".docx": "docx", ".csv": "csv",
};

/** The caps are powers of two, so `bytes / 1e6` renders 25 MiB as "26 MB". Divide by
 *  1024² and the label matches both the constant and the message the server sends back. */
function mb(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

/** ".pdf 25 MB · .docx 15 MB · …" — the per-type truth, not one number that is wrong for
 *  five of the seven types. */
function limitSummary(accepted: string[], maxBytes: Record<string, number>): string {
  const byKind = new Map<string, string[]>();
  for (const ext of accepted) {
    const kind = SUFFIX_KIND[ext] ?? "text";
    byKind.set(kind, [...(byKind.get(kind) ?? []), ext]);
  }
  return [...byKind.entries()]
    .sort((a, b) => (maxBytes[b[0]] ?? 0) - (maxBytes[a[0]] ?? 0))
    .map(([kind, exts]) => `${exts.join("/")} ${mb(maxBytes[kind] ?? FALLBACK_MAX)}`)
    .join(" · ");
}

export function KnowledgeView({ role, signedIn, onNeedSignIn, onChanged }: Props) {
  const [list, setList] = useState<KnowledgeList | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [rejected, setRejected] = useState<{ file: string; reasons: string[] } | null>(null);
  const [flash, setFlash] = useState<ApproveResult | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const canReview = REVIEW_ROLES.includes(role);

  const refresh = useCallback(() => {
    api.knowledgeDocs()
      .then((l) => { setList(l); setError(null); })
      .catch(setError);
  }, []);

  useEffect(() => { if (signedIn) refresh(); }, [signedIn, refresh]);

  const accepted = list?.accepted_types ?? [".pdf", ".txt", ".md", ".html", ".htm", ".docx", ".csv"];
  const maxBytes = list?.max_bytes ?? {};

  /**
   * Pre-flight only. Mirrors the server's allowlist and per-kind ceiling so an obvious
   * mistake costs a millisecond instead of a full upload — it is NOT the gate, and a
   * file that passes here can still be refused on content (magic bytes, zip ratio).
   */
  function preflight(file: File): string[] {
    const problems: string[] = [];
    const dot = file.name.lastIndexOf(".");
    const ext = dot > -1 ? file.name.slice(dot).toLowerCase() : "";
    if (!accepted.includes(ext)) {
      problems.push(`${ext || "no extension"} is not accepted — allowed: ${accepted.join(", ")}`);
    }
    // The cap for THIS file's kind, not the largest cap of any kind.
    const cap = maxBytes[SUFFIX_KIND[ext] ?? ""] ?? FALLBACK_MAX;
    if (file.size === 0) problems.push("the file is empty");
    else if (file.size > cap) {
      problems.push(`${mb(file.size)} is over the ${mb(cap)} limit for ${ext} files`);
    }
    return problems;
  }

  async function send(file: File) {
    if (!signedIn) { onNeedSignIn(); return; }
    setRejected(null);
    setFlash(null);
    const problems = preflight(file);
    if (problems.length) { setRejected({ file: file.name, reasons: problems }); return; }
    setBusy(true);
    try {
      const res = await api.knowledgeUpload(file);
      refresh();
      setSelected(res.doc_id);          // straight into review — that is the next step
    } catch (e) {
      // The upload validator returns every failure at once; render them as a list rather
      // than collapsing to one line, because a file can be wrong in several ways.
      const reasons = e instanceof ApiError && e.reasons.length
        ? e.reasons
        : [e instanceof Error ? e.message : String(e)];
      setRejected({ file: file.name, reasons });
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";   // allow re-picking the same file
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) send(file);
  }

  if (!signedIn) {
    return (
      <Banner tone="info" title="Sign in to manage knowledge documents">
        Uploading and approving documents needs an account with the{" "}
        <code>data_steward</code> or <code>admin</code> role. Reading VRR figures does not.
      </Banner>
    );
  }
  if (error) return <ErrorNote error={error} />;
  if (!list) return <Spinner label="loading the document registry…" />;

  const pending = list.documents.filter((d) => d.status === "pending_review");
  const indexed = list.documents.filter((d) => d.status === "approved" && (d.n_chunks ?? 0) > 0);
  // The user guide is generated by `make guide`, not curated here. Separating it stops
  // six files a steward never uploaded from reading as work they forgot to do.
  const live = indexed.filter((d) => d.doc_kind !== "app_help");
  const guide = indexed.filter((d) => d.doc_kind === "app_help");
  const refused = list.documents.filter((d) => d.status === "rejected");
  const { usage } = list;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Metric label="Searchable docs" value={live.length}
                foot={`${usage.chunks.toLocaleString()} chunks indexed`} />
        <Metric label="Awaiting review" value={pending.length}
                tone={pending.length ? "warn" : "plain"}
                foot={pending.length ? "not searchable yet" : "queue is clear"} />
        <Metric label="Document budget" value={`${usage.docs} / ${usage.max_docs}`}
                tone={usage.docs / usage.max_docs > 0.9 ? "warn" : "plain"}
                foot="a bounded index keeps top-k meaningful" />
        <Metric label="Chunk budget"
                value={`${Math.round((usage.chunks / usage.max_chunks) * 100)}%`}
                tone={usage.chunks / usage.max_chunks > 0.9 ? "warn" : "plain"}
                foot={`${usage.max_chunks.toLocaleString()} max`} />
      </div>

      {/* ---------------------------------------------------------------- upload */}
      {canReview ? (
        <Card title="Add a document"
              sub="It is stored but NOT searchable until you approve it below. Nothing is embedded on upload.">
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`rounded-lg border-2 border-dashed p-6 text-center transition ${
              dragging ? "border-brand-500 bg-brand-50" : "border-surface-border"}`}
          >
            <p className="text-body text-content-secondary">
              Drop a file here, or{" "}
              {/* A real input behind a label, not a div with onClick: this is what makes
                  the control reachable by keyboard and announced as a file input. */}
              <label className="cursor-pointer font-medium text-brand-600 underline underline-offset-2">
                choose one
                <input
                  ref={fileRef} type="file" className="sr-only"
                  accept={accepted.join(",")}
                  disabled={busy}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) send(f); }}
                />
              </label>
            </p>
            <p className="mt-2 text-label text-content-muted">
              {limitSummary(accepted, maxBytes)}
            </p>
            {busy && <p className="mt-3 text-label text-brand-600">uploading and validating…</p>}
          </div>

          {rejected && (
            <div className="mt-3 rounded-lg border border-offtarget/40 bg-offtarget-soft p-3">
              <p className="flex items-center gap-1.5 text-label font-medium text-offtarget">
                <StatusIcon kind="blocked" /> {rejected.file} was refused
              </p>
              <ul className="mt-1 list-disc pl-5 text-label text-offtarget">
                {rejected.reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}

          <p className="mt-3 text-label leading-relaxed text-content-muted">
            Checked server-side before anything is stored: extension allowlist, size cap,
            magic bytes against the extension, archive expansion ratio for{" "}
            <code>.docx</code>, path traversal in the filename, and a content hash so the
            same bytes cannot be indexed twice. The file picker's filter is a convenience,
            not the control.
          </p>
        </Card>
      ) : (
        <Banner tone="info" title="You can read this queue but not change it">
          Uploading and approving are limited to the <code>data_steward</code> and{" "}
          <code>admin</code> roles. Your role is <code>{role || "—"}</code>.
        </Banner>
      )}

      {flash && (
        <Banner tone={flash.searchable ? "good" : "warn"}
                title={flash.searchable
                  ? `${flash.file_name} is now searchable`
                  : `${flash.file_name} was approved but indexed nothing`}>
          {flash.searchable
            ? <>Indexed {flash.n_chunks} chunks from {flash.pages} page(s). Ask about it in
               the chat drawer — retrieval runs over pgvector and the answer will cite the
               file name and page.</>
            : <>The loader extracted no usable text, so no chunks were written. A scanned
               PDF with no text layer does this; it needs OCR before it can be indexed.</>}
        </Banner>
      )}

      {/* ---------------------------------------------------------------- review */}
      <Card title={`Awaiting review (${pending.length})`}
            sub="Read what the loader actually extracted, then decide. Approving embeds it immediately.">
        {pending.length === 0 ? (
          <p className="text-label text-content-muted">Nothing waiting. Uploads land here.</p>
        ) : (
          <ul className="space-y-2">
            {pending.map((d) => (
              <DocRow key={d.doc_id} doc={d} canReview={canReview}
                      open={selected === d.doc_id}
                      onToggle={() => setSelected(selected === d.doc_id ? null : d.doc_id)}
                      onDone={(res) => { if (res) setFlash(res); refresh(); onChanged?.(); }} />
            ))}
          </ul>
        )}
      </Card>

      {/* ------------------------------------------------------------------ live */}
      <Card title={`In the index (${live.length})`}
            sub="These are the only documents a chat question can reach.">
        {live.length === 0 ? (
          <p className="text-label text-content-muted">
            Nothing indexed yet — the agent will abstain on any document question.
          </p>
        ) : (
          <table className="w-full text-label">
            <thead>
              <tr className="border-b border-surface-divider text-micro uppercase tracking-wide text-content-muted">
                <th className="py-1.5 text-left font-medium">document</th>
                <th className="py-1.5 pr-4 text-right font-medium">chunks</th>
                <th className="py-1.5 text-left font-medium">PII</th>
                <th className="py-1.5 text-left font-medium">approved by</th>
                <th className="py-1.5" />
              </tr>
            </thead>
            <tbody>
              {live.map((d) => (
                <tr key={d.doc_id} className="border-b border-surface-divider last:border-0">
                  <td className="py-1.5 text-content-primary">
                    {d.file_name}
                    <span className="ml-1.5 text-content-muted">{d.content_kind}</span>
                  </td>
                  <td className="py-1.5 pr-4 text-right tabular-nums text-content-secondary">
                    {d.n_chunks}
                  </td>
                  <td className="py-1.5">
                    {d.pii_found
                      ? <Badge tone="amber">redacted: {d.pii_kinds}</Badge>
                      : <span className="text-content-muted">none</span>}
                  </td>
                  <td className="py-1.5 text-content-secondary">{d.reviewed_by ?? "—"}</td>
                  <td className="py-1.5 text-right">
                    {canReview && (
                      <button
                        onClick={async () => {
                          await api.knowledgeRemove(d.doc_id);
                          refresh(); onChanged?.();
                        }}
                        className="text-micro text-offtarget underline underline-offset-2">
                        remove from index
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="mt-3 text-label leading-relaxed text-content-muted">
          Removing drops the chunks and leaves the registry row: what was ingested, by
          whom, and whether it held PII stays on the record. Same rule as clearing a chat
          transcript — it hides, it does not erase.
        </p>
      </Card>

      {guide.length > 0 && (
        <Card title={`In-app user guide (${guide.length})`}
              sub="Generated from core/help_topics.py by `make guide` — a separate corpus, never mixed into a reservoir search.">
          <p className="text-label leading-relaxed text-content-muted">
            {guide.reduce((n, d) => n + (d.n_chunks ?? 0), 0)} chunks across{" "}
            {guide.map((d) => d.file_name).join(", ")}. These answer "how do I use this
            app?" in the chat drawer. To change them, edit the topic text and re-run{" "}
            <code className="font-mono">make guide</code> — editing the markdown by hand
            is overwritten.
          </p>
        </Card>
      )}

      {refused.length > 0 && (
        <Card title={`Rejected (${refused.length})`}
              sub="Kept so the same document is not re-uploaded next week.">
          <ul className="space-y-1 text-label">
            {refused.map((d) => (
              <li key={d.doc_id} className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-content-primary">{d.file_name}</span>
                <span className="text-content-muted">
                  by {d.reviewed_by ?? "—"}
                  {d.review_note ? ` — ${d.review_note}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

// --------------------------------------------------------------------- row ----
function DocRow({ doc, canReview, open, onToggle, onDone }: {
  doc: KnowledgeDoc; canReview: boolean; open: boolean;
  onToggle: () => void; onDone: (res: ApproveResult | null) => void;
}) {
  const [preview, setPreview] = useState<KnowledgePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!open || preview) return;
    setLoading(true);
    api.knowledgePreview(doc.doc_id)
      .then(setPreview).catch(setErr).finally(() => setLoading(false));
  }, [open, doc.doc_id, preview]);

  const piiKinds = Object.entries(preview?.pii_kinds ?? {});

  return (
    <li className="rounded-lg border border-surface-border">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <button onClick={onToggle}
                aria-expanded={open}
                className="flex-1 text-left text-body font-medium text-content-primary">
          {doc.file_name}
          <span className="ml-2 text-label font-normal text-content-muted">
            {doc.content_kind} · {((doc.size_bytes ?? 0) / 1e3).toFixed(0)} KB · from{" "}
            {doc.uploaded_by ?? doc.source}
          </span>
        </button>
        <Badge tone="amber">not searchable</Badge>
        <button onClick={onToggle} className="text-micro text-brand-600 underline underline-offset-2">
          {open ? "hide" : "review"}
        </button>
      </div>

      {doc.ingest_error && (
        <p className="border-t border-surface-divider px-3 py-2 text-label text-offtarget">
          Last approval failed and was rolled back: {doc.ingest_error}
        </p>
      )}

      {open && (
        <div className="space-y-3 border-t border-surface-divider p-3">
          {loading && <Spinner label="extracting text…" />}
          {err ? <ErrorNote error={err} /> : null}
          {preview && (
            <>
              <div className="flex flex-wrap gap-3 text-label text-content-secondary">
                <span><strong className="tabular-nums">{preview.pages}</strong> page(s)</span>
                <span><strong className="tabular-nums">{preview.n_chunks}</strong> chunks
                  ({preview.strategy})</span>
                <span><strong className="tabular-nums">
                  {preview.total_chars.toLocaleString()}</strong> chars extracted</span>
              </div>

              {preview.empty_extraction && (
                <Banner tone="bad" title="This document extracts to almost no text">
                  Approving it would write chunks of nothing into the index, where they
                  match every query weakly and crowd out real answers. A scanned PDF needs
                  an OCR pass first.
                </Banner>
              )}

              {piiKinds.length > 0 && (
                <Banner tone="warn" title="PII detected — it will be redacted before embedding">
                  {piiKinds.map(([k, n]) => `${n}× ${k}`).join(", ")}. Each match is
                  replaced with <code>[REDACTED:kind]</code>, so the raw value never
                  reaches Postgres or the vector index. The preview below is redacted too.
                </Banner>
              )}

              <div>
                <p className="mb-1 text-micro font-medium uppercase tracking-wide text-content-muted">
                  extracted text — this is what gets embedded
                </p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-surface-raised p-2 text-label leading-relaxed text-content-secondary">
                  {preview.extracted_text || "(nothing extracted)"}
                  {preview.truncated ? "\n\n… truncated" : ""}
                </pre>
              </div>

              {canReview && (
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    disabled={acting}
                    onClick={async () => {
                      setActing(true);
                      try { onDone(await api.knowledgeApprove(doc.doc_id)); }
                      catch (e) { setErr(e); }
                      finally { setActing(false); }
                    }}
                    className="rounded-md bg-brand-500 px-3 py-1.5 text-label font-medium text-surface-base disabled:opacity-50">
                    {acting ? "embedding…" : "Approve & embed"}
                  </button>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value.slice(0, 500))}
                    maxLength={500}
                    placeholder="reason (optional)"
                    aria-label="Rejection reason"
                    // max-w keeps the row from stretching to the right edge, where the
                    // floating "Ask the agent" launcher sits — full-width put Reject
                    // underneath it and made the button unclickable.
                    className="min-w-0 max-w-xs flex-1 rounded-md border border-surface-border px-2 py-1.5 text-label placeholder:text-content-muted"
                  />
                  <button
                    disabled={acting}
                    onClick={async () => {
                      setActing(true);
                      try { await api.knowledgeReject(doc.doc_id, note); onDone(null); }
                      catch (e) { setErr(e); }
                      finally { setActing(false); }
                    }}
                    className="rounded-md border border-surface-border px-3 py-1.5 text-label text-content-secondary disabled:opacity-50">
                    Reject
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}
