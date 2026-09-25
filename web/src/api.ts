/**
 * The only place this app talks to the backend.
 *
 * Types mirror what `agent/tools.py` returns. They are hand-written rather than
 * generated because the tool payloads carry provenance keys (`sources`, `run_id`,
 * `pvt_methods`, `formulas`) that matter to the UI, and a generator would flatten them
 * into `any` — the fields most worth typing are exactly the ones proving where a number
 * came from.
 *
 * Nothing here computes. If a value needs deriving, it is derived in `core/` behind a
 * tool, so the browser and the LLM cannot disagree about a figure.
 */

const BASE = "/api";

/**
 * The bearer token, held in localStorage so a refresh does not sign you out.
 *
 * Honest limitation: localStorage is readable by any script on the page, so an XSS bug
 * leaks the token. The alternative — an httpOnly cookie — is immune to that but needs
 * CSRF protection, and for a workbench you run on your own machine the trade is fine.
 * It would NOT be fine on a shared deployment; that is the point to switch.
 */
const TOKEN_KEY = "vrr.token";

export const session = {
  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  },
  save(token: string) {
    localStorage.setItem(TOKEN_KEY, token);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
  },
};

/** Fires when the server rejects our token, so the shell can show the login form. */
export const onUnauthorized: { handler: (() => void) | null } = { handler: null };

function authHeaders(): Record<string, string> {
  const t = session.token;
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function get<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined) qs.set(k, v);
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ""}`;
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw await fail(res, url);
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw await fail(res, path);
  return res.json() as Promise<T>;
}

/**
 * Multipart POST. Deliberately does NOT set Content-Type: the browser has to generate it
 * itself so it can append the `boundary=` parameter, and setting it by hand produces a
 * body the server cannot parse.
 */
async function postForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST", headers: authHeaders(), body: form,
  });
  if (!res.ok) throw await fail(res, path);
  return res.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw await fail(res, path);
  return res.json() as Promise<T>;
}

/** A 401 means the token is gone, expired or forged — drop it and ask for a new one. */
async function fail(res: Response, path: string): Promise<ApiError> {
  const raw = await rawDetail(res);
  const err = new ApiError(res.status, flatten(raw) || res.statusText, path, raw);
  if (res.status === 401) {
    session.clear();
    onUnauthorized.handler?.();
  }
  return err;
}

async function rawDetail(res: Response): Promise<unknown> {
  try {
    return (await res.json()).detail;
  } catch {
    return null;
  }
}

/**
 * Turn a FastAPI `detail` into one readable line.
 *
 * It arrives in three shapes and they are not interchangeable: a plain string from
 * `HTTPException`, a LIST of `{loc, msg}` from a Pydantic 422, and — from the upload
 * endpoint — an object carrying every validation failure at once. `JSON.stringify` on
 * the last two produced `[object Object]`-grade noise in the UI, which is how a precise
 * server-side rejection reached the user as an unreadable blob.
 */
function flatten(detail: unknown): string {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const e = d as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : undefined;
        return field ? `${field}: ${e.msg ?? ""}` : (e.msg ?? String(d));
      })
      .join("; ");
  }
  const o = detail as { rejected?: string[] };
  if (Array.isArray(o.rejected)) return o.rejected.join("; ");
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public path: string,
              /** The unflattened body, so a caller can render each reason on its own row. */
              public detail: unknown = null) {
    super(message);
  }
  /** Every rejection reason from the upload validator, or [] for other failures. */
  get reasons(): string[] {
    const o = this.detail as { rejected?: string[] } | null;
    return Array.isArray(o?.rejected) ? o.rejected : [];
  }
}

// ---------------------------------------------------------------- types ----
export interface Pattern {
  pattern_id: string;
  pattern_name: string;
  asset?: string | null;
  vrr?: number | null;
  vrr_date?: string | null;
}

export interface TrendRow {
  vrr_date: string;
  vrr: number;
  inj_res_bbl: number;
  prod_res_bbl: number;
  any_extrapolated: boolean;
  n_completions: number;
}

export interface PatternContext {
  pattern_id: string;
  pattern_name: string;
  asset?: string | null;
  target_vrr: number;
  memory?: { typical_low?: number | null; typical_high?: number | null;
             response_factor?: number | null } | null;
}

export interface OverviewRow {
  id_pattern: string;
  pattern_name: string;
  asset?: string | null;
  vrr: number;
  target_vrr: number;
  drift: number;
  verdict?: string | null;
  n_completions: number;
  any_extrapolated: boolean;
  response_factor?: number | null;
  vrr_date: string;
}

export interface Overview {
  n_patterns: number;
  off_target: unknown[];
  patterns: OverviewRow[];
}

export interface Driver {
  term: string;
  label: string;
  contribution: number;
  share: number;
  d_res_bbl?: number;
}

export interface Decompose {
  ok: boolean;
  reason?: string;
  vrr_a: number;
  vrr_b: number;
  d_vrr: number;
  drivers: Driver[];
  side_contributions: { injection: number; production: number };
}

export interface AuditResult {
  ok: boolean;
  stored: { vrr: number; run_id?: string | null };
  recomputed: { vrr: number };
  difference: number;
  matches: boolean;
  n_raw_rows: number;
  pvt_methods: string[];
  low_confidence_inputs: boolean;
  provenance: { recomputed_from: string[]; code: string };
}

export interface Lineage {
  sources: Record<string, string>;
  /** FACTOR summarised across the period's completions — it is the first multiplicand
   *  in every formula, so the graph shows it as numbers, not just as a table name. */
  allocation?: { n: number; min: number | null; max: number | null;
                 weighted_mean: number | null };
  formulas: Record<string, string>;
  completions: Record<string, unknown>[];
  term_totals: Record<string, number>;
  recomputed_from_terms: { prod_res_bbl: number; inj_res_bbl: number; vrr: number };
}

/** The pattern schematic. Every x/y is computed by `core.pattern_layout` — this file
 *  carries the shape, it does not decide it. `is_schematic` is always true today and the
 *  view must say so: the database holds contribution factors, never coordinates. */
export interface LayoutNode {
  completion_id: string;
  completion_name: string;
  role: "injector" | "producer" | "idle";
  x: number; y: number;
  factor: number; share: number; size: number; res_bbl: number;
  shared: boolean; n_patterns: number;
  low_confidence: boolean; pvt_methods: string;
}

export interface PatternLayout {
  found: boolean;
  pattern_id?: string; pattern_name?: string; vrr_date?: string; vrr?: number;
  prod_res_bbl?: number; inj_res_bbl?: number;
  geometry?: string; geometry_label?: string; caption?: string; is_schematic?: boolean;
  n_injectors?: number; n_producers?: number; n_idle?: number;
  nodes?: LayoutNode[];
  links?: { to: string; factor: number; share_of_production: number }[];
  hub?: { x: number; y: number; radius: number };
  shared?: string[]; low_confidence?: string[];
}

export interface AnalysisCase {
  ok: boolean;
  reason?: string;
  narrative: string;
  draft?: { action_type: string; severity?: string } | null;
  audit?: { audit?: { verdict?: string; summary?: string } };
}

export interface QueueItem {
  action_id: string;
  id_pattern: string;
  pattern_name: string;
  vrr_date: string;
  stage: string;
  severity: string;
  confidence?: string;
  action_type: string;
  driver?: string | null;
  narrative?: string | null;
  stage_by?: string | null;
  run_id?: string | null;
  recommendation?: { injector_changes?: Record<string, unknown>[] } | string | null;
}

export interface Adjustment {
  action_id: string;
  pattern_name: string;
  vrr_date: string;
  change_type: string;
  d_surface_pct?: number | null;
  pre_vrr?: number | null;
  predicted_post_vrr?: number | null;
  actual_post_vrr?: number | null;
  approved_by?: string | null;
  ts: string;
}

/** The provenance block every answer carries — what the caption in the drawer renders. */
export interface ChatMeta {
  llm?: boolean;
  model?: string | null;
  gate?: string | null;
  retrieved?: number;
  grounded?: boolean;
  tools_called?: string[] | null;
  violations?: { kind: string; term?: string; detail: string }[] | null;
  first_attempt_violations?: { kind: string; term?: string; detail: string }[] | null;
  uncited_numbers?: number[] | null;
}

export interface ChatAnswer {
  intent: string;
  text: string;
  meta: ChatMeta;
  data?: unknown;
  persisted?: boolean;
  /** Tracing is meant to be on always; these say whether this turn actually landed
   *  in MLflow, and link straight to its span tree. */
  trace_id?: string | null;
  trace_url?: string | null;
  traced?: boolean;
}

export interface HistoryTurn {
  chat_id: string;
  question: string;
  answer: string;
  intent: string;
  asked_by?: string | null;
  created_at: string;
  meta?: ChatMeta | null;
  payload?: unknown;
}

export interface Health {
  auth: { required_for: string[]; scheme: string; token_ttl_minutes: number;
          ephemeral_secret: boolean };
  llm: { available: boolean; model: string | null; provider: string };
  tracing: { enabled: boolean; uri: string };
  postgres: { host: string; monthly_rows: number; n_patterns?: number | null };
  knowledge: { docs: number; chunks: number; pending_review?: number };
  retrieval_min_score: number;
}

// -------------------------------------------------------------- architecture ----
/**
 * The system describing itself. Geometry comes from `core/architecture.py` for the same
 * reason the pattern schematic does — so the figure is unit-tested off-DB and React
 * cannot quietly disagree with it about where a box goes.
 *
 * `value` is `null` when the fact behind a box could not be measured. Render nothing in
 * that case; do NOT substitute a zero or a dash that reads like a measurement.
 */
export interface ArchNode {
  id: string;
  band: string;
  label: string;
  value: string | null;
  what: string;
  files: string[];
  guardrail: string;
  x: number; y: number; w: number; h: number;
}

export interface ArchBand {
  id: string; title: string; sub: string;
  x: number; y: number; w: number; h: number;
}

export interface Architecture {
  canvas: { w: number; h: number };
  bands: ArchBand[];
  nodes: ArchNode[];
  edges: { from: string; to: string; label: string }[];
}

// ------------------------------------------------------- knowledge upload ----
/** A row in the registry. `status` is the gate: only `approved` with n_chunks > 0 is
 *  reachable by a chat question — everything else is invisible to search. */
export interface KnowledgeDoc {
  doc_id: string;
  file_name: string;
  status: "pending_review" | "approved" | "rejected";
  source: string;
  uploaded_by?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_note?: string | null;
  ingest_error?: string | null;
  content_kind?: string | null;
  /** Which corpus. `app_help` rows are the generated user guide, owned by `make guide` —
   *  they are not documents a steward uploaded and must not read as review backlog. */
  doc_kind?: string;
  size_bytes?: number | null;
  n_chunks?: number | null;
  pii_found?: boolean | null;
  pii_kinds?: string | null;
  registered_at: string;
}

export interface KnowledgeList {
  documents: KnowledgeDoc[];
  usage: { docs: number; chunks: number; max_docs: number; max_chunks: number };
  can_review: boolean;
  accepted_types: string[];
  max_bytes: Record<string, number>;
}

/** What the reviewer reads before approving. Text is PII-redacted server-side. */
export interface KnowledgePreview {
  doc_id: string;
  file_name: string;
  pages: number;
  n_chunks: number;
  total_chars: number;
  strategy: string;
  extracted_text: string;
  truncated: boolean;
  pii_kinds: Record<string, number>;
  /** A PDF with no text layer chunks into noise — the reviewer has to be told. */
  empty_extraction: boolean;
  status: string;
  uploaded_by?: string | null;
}

export interface UploadResult {
  doc_id: string; file_name: string; status: string; kind: string;
  size_bytes: number; sha256: string; warnings: string[]; uploaded_by: string;
  next: string;
}

export interface ApproveResult {
  doc_id: string; file_name: string; n_chunks: number; pages: number;
  pii_kinds: string[]; status: string; reviewed_by: string;
  searchable: boolean; note: string; already?: boolean;
}

/** Every lane at once — the swim-lane board reads this in one call. */
export interface Board {
  lanes: Record<string, QueueItem[]>;
  counts: Record<string, number>;
  approver_for_stage: Record<string, string>;
  order: string[];
}

export interface Stages {
  stages: string[];
  roles: string[];
  approver_for_stage: Record<string, string>;
}

// ------------------------------------------------------------- endpoints ----
export interface Identity {
  username: string;
  role: string;
  full_name?: string | null;
}

export const api = {
  /** OAuth2 password grant. Form-encoded, not JSON — that is what the spec (and
   *  FastAPI's OAuth2PasswordRequestForm, and the /docs Authorize button) expects. */
  async login(username: string, password: string): Promise<Identity> {
    const res = await fetch(`${BASE}/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) {
      const raw = await rawDetail(res);
      throw new ApiError(res.status, flatten(raw) || res.statusText, "/auth/token", raw);
    }
    const body = await res.json();
    session.save(body.access_token);
    return { username: body.username, role: body.role, full_name: body.full_name };
  },
  me: () => get<{ username: string; role: string; expires_at: number }>("/auth/me"),
  logout: () => session.clear(),

  health: () => get<Health>("/health"),

  /** The system's own architecture, with every counter measured at request time. */
  architecture: () => get<Architecture>("/architecture"),
  stages: () => get<Stages>("/stages"),

  patterns: () => get<Pattern[]>("/patterns"),
  overview: (asset?: string) => get<Overview>("/overview", { asset }),
  dataQuality: () => get<{ ok: boolean; n_findings?: number; checks_run: string[];
                           findings?: Record<string, Record<string, unknown>[]> }>("/data-quality"),
  inputAudit: (pattern?: string) =>
    get<{ n?: number; by_verdict?: Record<string, number>;
          audits?: { vrr_date: string; verdict: string; summary: string }[] }>(
      "/input-audit", { pattern }),

  context: (id: string) => get<PatternContext>(`/patterns/${id}/context`),
  trend: (id: string) => get<{ rows: TrendRow[] }>(`/patterns/${id}/trend`),
  decompose: (id: string, from: string, to: string) =>
    get<Decompose>(`/patterns/${id}/decompose`, { from, to }),
  audit: (id: string, date: string) => get<AuditResult>(`/patterns/${id}/audit`, { date }),
  lineage: (id: string, date: string) => get<Lineage>(`/patterns/${id}/lineage`, { date }),
  analysis: (id: string, date: string) => get<AnalysisCase>(`/patterns/${id}/analysis`, { date }),
  layout: (id: string, date: string) => get<PatternLayout>(`/patterns/${id}/layout`, { date }),
  // No submitted_by / role / user in any write: the server takes the actor from the
  // token. A client-supplied identity on an audit trail is a signature anyone can forge.
  submit: (id: string, date: string) =>
    post<{ action_id: string; next_approver: string }>(`/patterns/${id}/submit`, { date }),

  queue: (stage: string) => get<QueueItem[]>("/queue", { stage }),
  board: () => get<Board>("/board"),
  adjustments: () => get<Adjustment[]>("/adjustments"),
  advance: (actionId: string) =>
    post<{ from: string; to: string; by: string; wrote_adjustment_history: boolean }>(
      `/queue/${actionId}/advance`, undefined),
  reject: (actionId: string) =>
    post<{ to: string }>(`/queue/${actionId}/reject`, undefined),

  // Knowledge upload + review. Nothing uploaded here is searchable until a data steward
  // approves it — the server keeps that gate; these calls only drive it.
  knowledgeDocs: (status?: string) => get<KnowledgeList>("/knowledge/documents", { status }),
  knowledgeUpload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<UploadResult>("/knowledge/upload", form);
  },
  knowledgePreview: (docId: string) =>
    get<KnowledgePreview>(`/knowledge/documents/${docId}/preview`),
  knowledgeApprove: (docId: string) =>
    post<ApproveResult>(`/knowledge/documents/${docId}/approve`, undefined),
  knowledgeReject: (docId: string, note: string) =>
    post<{ doc_id: string; status: string }>(
      `/knowledge/documents/${docId}/reject?note=${encodeURIComponent(note)}`, undefined),
  knowledgeRemove: (docId: string) =>
    del<{ doc_id: string; chunks_removed: number }>(`/knowledge/documents/${docId}`),

  chat: (body: { question: string; pattern?: string; date?: string; agentic?: boolean }) =>
    post<ChatAnswer>("/chat", body),
  // No `user` param: whose "cleared" cutoff to apply is taken from the bearer token
  // server-side. It used to be a query string, which is a client-asserted identity.
  history: (pattern: string) => get<HistoryTurn[]>("/chat/history", { pattern }),
  /** Hides the transcript for THIS user. Deletes nothing — the rows and the traces
   *  behind them are the audit record. */
  clearChat: (pattern: string) =>
    post<{ cleared_for: string; note: string }>(`/chat/clear?pattern=${pattern}`, undefined),
};
