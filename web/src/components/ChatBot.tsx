/**
 * The analyst chatbot: a launcher bottom-right, opening a panel that FLOATS over the
 * page. It used to be a docked column taking a third of the screen, which made every
 * view narrower whether or not you were talking to it.
 *
 * Two properties that are not cosmetic:
 *
 * - **Every turn is traced.** The answer carries its MLflow trace id, and each reply
 *   links to the span tree behind it — tools, LLM call, gate verdict. When MLflow is
 *   unreachable the turn is marked NOT TRACED rather than quietly losing the record.
 * - **Clearing hides, never deletes.** "Clear" records a per-user cutoff server-side.
 *   The rows stay in `vrr_agent.chat_history`, other people still see the full
 *   transcript, and every question remains in MLflow. An audit trail you can erase from
 *   the UI is not an audit trail.
 */
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type ChatMeta, type HistoryTurn } from "../api";
import { StatusIcon, provenanceLine, violationLine } from "./ui";

interface Props {
  patternId: string;
  patternName: string;
  period: string;
  user: string;
  vsTarget: "high" | "low";
  signedIn: boolean;
  onNeedSignIn: () => void;
  llmUp: boolean;
}

interface Turn {
  question: string; answer: string; intent: string; meta: ChatMeta;
  askedBy?: string | null; at?: string; payload?: unknown;
  traceUrl?: string | null; traced?: boolean; pending?: boolean;
}

/** Mirrors `api/schemas.py::ChatRequest.question` — kept equal to it on purpose. */
const QUESTION_MAX = 2000;

export function ChatBot({ patternId, patternName, period, user, vsTarget, signedIn,
                          onNeedSignIn, llmUp }: Props) {
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [agentic, setAgentic] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!patternId) return;
    api.history(patternId)
      .then((rows: HistoryTurn[]) => setTurns(rows.map(fromHistory)))
      .catch(() => setTurns([]));
  }, [patternId, user]);

  useEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
    if (nearBottom || busy) box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  }, [turns.length, busy, open]);

  async function ask(question: string) {
    if (!question.trim() || busy) return;
    if (!signedIn) { onNeedSignIn(); return; }
    setInput("");
    setBusy(true);
    setTurns((t) => [...t, { question, answer: "", intent: "", meta: {}, pending: true }]);
    try {
      const res = await api.chat({ question, pattern: patternId, date: period, agentic });
      setTurns((t) => [...t.slice(0, -1), {
        question, answer: res.text, intent: res.intent, meta: res.meta ?? {},
        askedBy: user, at: new Date().toISOString(), payload: res.data,
        traceUrl: res.trace_url, traced: res.traced,
      }]);
    } catch (e) {
      setTurns((t) => [...t.slice(0, -1), {
        question, answer: `Request failed: ${e instanceof Error ? e.message : String(e)}`,
        intent: "error", meta: {}, askedBy: user,
      }]);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    try {
      await api.clearChat(patternId);
      setTurns([]);
      setConfirmClear(false);
    } catch (e) {
      setConfirmClear(false);
      console.error(e);
    }
  }

  const nearLimit = input.length > QUESTION_MAX * 0.8;

  const quick: [string, string][] = [
    ["Why this VRR?", `Why is ${patternName}'s VRR ${vsTarget} in ${monthName(period)}?`],
    ["Is it correct?", `Is the ${monthName(period)} number actually correct?`],
    ["How computed?", `How is ${patternName}'s VRR calculated?`],
    ["Documents", "What do the documents say about changing injection rates?"],
  ];

  // ----------------------------------------------------------------- launcher
  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        title={`Ask about ${patternName}`}
        className="fixed bottom-5 right-5 z-30 flex h-12 items-center gap-2 rounded-full bg-brand-500 px-4 text-body font-medium text-surface-base shadow-panel transition hover:bg-brand-600"
      >
        <ChatIcon />
        Ask the agent
        {turns.length > 0 && (
          <span className="rounded-full bg-surface-card/20 px-1.5 text-micro tabular-nums">
            {turns.length}
          </span>
        )}
      </button>
    );
  }

  // -------------------------------------------------------------------- panel
  return (
    <div className="fixed bottom-5 right-5 z-30 flex h-[min(38rem,calc(100vh-6rem))] w-[min(26rem,calc(100vw-2.5rem))] flex-col overflow-hidden rounded-xl border border-surface-border bg-surface-card shadow-panel">
      <header className="flex shrink-0 items-center gap-2 border-b border-surface-divider bg-brand-500 px-3 py-2.5 text-surface-base">
        <ChatIcon />
        <div className="min-w-0 flex-1 leading-tight">
          <div className="truncate text-label font-semibold">Ask about {patternName}</div>
          <div className="text-micro text-brand-100">
            {monthName(period)} · numbers stay tool-computed
          </div>
        </div>
        {turns.length > 0 && (
          <button onClick={() => setConfirmClear(true)} title="Clear this conversation"
                  className="rounded px-1.5 py-1 text-micro text-brand-100 hover:bg-surface-card/10">
            clear
          </button>
        )}
        <button onClick={() => setOpen(false)} aria-label="Close"
                className="rounded px-1.5 py-1 text-brand-100 hover:bg-surface-card/10">✕</button>
      </header>

      {confirmClear && (
        <div className="shrink-0 border-b border-suspect/40 bg-suspect-soft px-3 py-2.5">
          <p className="text-micro leading-relaxed text-suspect">
            Hide this conversation <strong>for you</strong>. Nothing is deleted — the rows
            stay in the shared transcript for everyone else, and every question remains in
            MLflow as a trace.
          </p>
          <div className="mt-2 flex gap-2">
            <button onClick={clear}
                    className="rounded bg-suspect px-2 py-1 text-micro font-medium text-surface-base">
              Hide for me
            </button>
            <button onClick={() => setConfirmClear(false)}
                    className="rounded border border-amber-300 px-2 py-1 text-micro text-suspect">
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="shrink-0 border-b border-surface-divider px-3 py-2">
        <label className="flex items-center gap-2 text-micro text-content-secondary">
          <input type="checkbox" checked={agentic} disabled={!llmUp}
                 className="h-3 w-3"
                 onChange={(e) => setAgentic(e.target.checked)} />
          Model picks the tools itself
          <span className="text-content-muted">{agentic ? "~1–2 min" : "~8 s"}</span>
        </label>
        <div className="mt-1.5 flex flex-wrap gap-1">
          {quick.map(([label, prompt]) => (
            <button key={label} onClick={() => ask(prompt)} disabled={busy}
                    className="rounded-full border border-surface-border px-2 py-0.5 text-micro text-content-secondary hover:border-brand-300 hover:bg-brand-50 disabled:opacity-40">
              {label}
            </button>
          ))}
        </div>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-2.5 overflow-y-auto bg-surface-raised p-3">
        {turns.length === 0 && (
          <p className="py-6 text-center text-micro text-content-muted">
            Nothing asked about this pattern yet.
          </p>
        )}
        {turns.map((t, i) => <TurnBlock key={i} turn={t} />)}
      </div>

      {/*
        The length cap mirrors `ChatRequest.question` (max_length=2000) server-side. Both
        exist on purpose: this one turns a 422 into a counter the user can see BEFORE
        sending, and the server one is the actual control — the input is a hint, and any
        HTTP client bypasses it.
      */}
      <form className="shrink-0 border-t border-surface-divider p-2"
            onSubmit={(e) => { e.preventDefault(); ask(input); }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value.slice(0, QUESTION_MAX))}
          maxLength={QUESTION_MAX}
          aria-label={`Ask about ${patternName}`}
          aria-describedby={nearLimit ? "chat-len" : undefined}
          placeholder={signedIn ? `Ask about ${patternName}…` : "Sign in to ask…"}
          disabled={busy}
          className="w-full rounded-lg border border-surface-border px-3 py-2 text-body placeholder:text-content-muted focus:border-brand-500 disabled:bg-surface-raised"
        />
        {nearLimit && (
          <p id="chat-len" className="mt-1 px-1 text-right text-micro tabular-nums text-content-muted">
            {input.length} / {QUESTION_MAX}
          </p>
        )}
      </form>
    </div>
  );
}

function TurnBlock({ turn }: { turn: Turn }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const caught = turn.meta.violations ?? turn.meta.first_attempt_violations ?? [];
  const rejected = !!turn.meta.violations;

  return (
    <div className="space-y-1">
      <div className="ml-6 rounded-lg rounded-br-sm bg-brand-500 px-2.5 py-1.5 text-body text-surface-base">
        {turn.question}
      </div>

      {turn.pending ? (
        <p className="px-1 text-micro text-content-muted">thinking…</p>
      ) : (
        <>
          <div className="prose-agent mr-4 rounded-lg rounded-bl-sm border border-surface-border bg-surface-card px-2.5 py-2 text-body">
            <Markdown remarkPlugins={[remarkGfm]}>{turn.answer}</Markdown>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 px-1 text-micro text-content-muted">
            <span className="inline-flex items-baseline gap-1">
              {(() => {
                const p = provenanceLine(turn.intent, turn.meta);
                const tone = { ok: "text-signal", warn: "text-suspect",
                               idle: "text-content-muted", none: "" }[p.tone];
                return (<>
                  {p.tone !== "none" && (
                    <StatusIcon kind={p.tone === "warn" ? "warn" : p.tone === "idle" ? "idle" : "ok"}
                                className={tone} />
                  )}
                  <span>{p.text}</span>
                </>);
              })()}
            </span>
            {turn.traceUrl ? (
              <a href={turn.traceUrl} target="_blank" rel="noreferrer"
                 className="text-brand-600 underline underline-offset-2">
                trace ↗
              </a>
            ) : turn.traced === false ? (
              <span className="font-medium text-offtarget">NOT TRACED</span>
            ) : null}
            {(caught.length > 0 || Boolean(turn.payload)) && (
              <button onClick={() => setShowEvidence((s) => !s)}
                      className="underline underline-offset-2">
                {showEvidence ? "hide evidence" : "evidence"}
              </button>
            )}
          </div>

          {showEvidence && (
            <div className="mr-4 space-y-2 px-1">
              {caught.length > 0 && (
                <div className="rounded-md border border-suspect/40 bg-suspect-soft p-2">
                  <p className="text-micro font-medium text-suspect">
                    {rejected
                      ? "Gate rejected the model's phrasing — computed wording shown:"
                      : "Gate caught this and had the model rewrite it:"}
                  </p>
                  <ul className="mt-1 list-disc pl-4 text-micro text-suspect">
                    {caught.map((v, i) => <li key={i}>{violationLine(v)}</li>)}
                  </ul>
                </div>
              )}
              {turn.payload ? (
                <pre className="max-h-52 overflow-auto rounded-md bg-surface-card p-2 text-micro text-content-secondary ring-1 ring-slate-200">
                  {JSON.stringify(turn.payload, null, 2)}
                </pre>
              ) : null}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function fromHistory(r: HistoryTurn): Turn {
  return {
    question: r.question, answer: r.answer, intent: r.intent, meta: r.meta ?? {},
    askedBy: r.asked_by, at: r.created_at, payload: r.payload,
  };
}

function monthName(iso: string): string {
  if (!iso) return "";
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined,
    { month: "long", year: "numeric" });
}

function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden>
      <path d="M10 2c4.4 0 8 2.9 8 6.5S14.4 15 10 15c-.9 0-1.8-.1-2.6-.4L3 16l1.2-3.1
               C2.8 11.7 2 10.2 2 8.5 2 4.9 5.6 2 10 2z" />
    </svg>
  );
}
