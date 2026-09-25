/**
 * Approval board — where every proposed change is, at a glance.
 *
 * Swim lanes rather than a filtered list, because the question this view answers is
 * "where is work stuck?", and a dropdown showing one stage at a time cannot answer it.
 * Lane colour matches the stage colour used everywhere else, so a card tells you where
 * it sits before you read it.
 *
 * Advisory only: the agent writes `draft` and every forward step is a human act. The
 * buttons hide when the stage is not yours — but that is convenience. The SERVER refuses
 * a transition your token's role does not own, so hiding is not what makes it safe.
 *
 * **Cards drag, but the chain does not bend.** Exactly one lane is ever a legal target:
 * the next one, and only when your token's role owns the current stage. That is not a
 * simplification of a scrum board, it is the difference between this board and one —
 * Jira lets you drag anything anywhere because its columns are a workflow; these columns
 * are an approval chain ending in a valve change, and "analyst drags straight to
 * executed" is precisely the move it exists to prevent. Every other lane is inert, so an
 * illegal drop cannot be *attempted* rather than being attempted and refused.
 *
 * Dragging is an accelerator, never the control:
 *   - the drop calls the SAME `POST /queue/{id}/advance` the button calls — no new
 *     endpoint, no second permission path to keep in sync with the first;
 *   - the server still decides, and a refusal returns the card to its lane with the
 *     reason shown rather than snapping back silently;
 *   - the Approve/Reject buttons stay. A drag-only board is unusable by keyboard, which
 *     is the failure the July audit found across ~19 buttons and will not reintroduce.
 */
import { useCallback, useEffect, useState } from "react";
import { api, type Adjustment, type Board, type QueueItem } from "../api";
import { Badge, Banner, Card, DataTable, ErrorNote, Spinner, fmt } from "../components/ui";

const LANE_STYLE: Record<string, { dot: string; head: string; ring: string }> = {
  draft: { dot: "bg-stage-draft", head: "text-content-secondary", ring: "ring-stage-draft/30" },
  analyst: { dot: "bg-stage-analyst", head: "text-brand-500", ring: "ring-stage-analyst/30" },
  rm: { dot: "bg-stage-rm", head: "text-stage-rm", ring: "ring-stage-rm/30" },
  site: { dot: "bg-stage-site", head: "text-suspect", ring: "ring-stage-site/30" },
  executed: { dot: "bg-stage-executed", head: "text-signal", ring: "ring-stage-executed/30" },
  rejected: { dot: "bg-stage-rejected", head: "text-offtarget", ring: "ring-stage-rejected/30" },
};

const LANE_HINT: Record<string, string> = {
  draft: "raised by the agent — awaiting analyst review",
  analyst: "analyst signed off — awaiting RM",
  rm: "RM signed off — awaiting site engineer",
  site: "site signed off — ready to execute",
  executed: "written to adjustment_history; the ρ loop reads these",
  rejected: "closed without action",
};

export function ApprovalView({ role }: { role: string }) {
  const [board, setBoard] = useState<Board | null>(null);
  const [history, setHistory] = useState<Adjustment[]>([]);
  const [open, setOpen] = useState<QueueItem | null>(null);
  const [note, setNote] = useState<{ tone: "good" | "bad"; text: string } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  /** The card under the cursor, and the one lane it may legally be dropped on. */
  const [drag, setDrag] = useState<{ item: QueueItem; target: string } | null>(null);
  const [over, setOver] = useState<string | null>(null);
  /** action_id being POSTed, so its card can show it is in flight and not be re-dropped. */
  const [moving, setMoving] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.board().then(setBoard).catch(setErr);
    api.adjustments().then(setHistory).catch(() => {});
  }, []);

  useEffect(refresh, [refresh]);

  if (err) return <ErrorNote error={err} />;
  if (!board) return <Spinner label="loading the board…" />;

  async function act(item: QueueItem, kind: "advance" | "reject") {
    try {
      const res = kind === "advance"
        ? await api.advance(item.action_id)
        : await api.reject(item.action_id);
      setNote({
        tone: "good",
        text: kind === "advance"
          ? `${item.action_id} → ${res.to}` +
            ("wrote_adjustment_history" in res && res.wrote_adjustment_history
              ? " · written to adjustment_history" : "")
          : `${item.action_id} rejected`,
      });
      setOpen(null);
      refresh();
    } catch (e) {
      setNote({ tone: "bad", text: e instanceof Error ? e.message : String(e) });
    }
  }

  /**
   * May THIS user drag THIS card, and if so, onto which single lane?
   *
   * Returns null for "not draggable", which covers three different situations that all
   * mean the same thing to the board: a terminal stage, a stage this role does not own,
   * and a signed-out reader. Keeping them one answer here is what makes the render below
   * unable to accidentally offer a drop that the server would refuse.
   */
  function legalTarget(item: QueueItem): string | null {
    if (!board) return null;
    if (item.stage === "executed" || item.stage === "rejected") return null;
    if (board.approver_for_stage[item.stage] !== role) return null;
    const nxt = nextOf(board.order, item.stage);
    // `order` ends with "rejected", so the last real stage's "next" is that — a forward
    // drag must never mean rejection. Rejecting stays an explicit, deliberate click.
    return nxt === "rejected" || nxt === "—" ? null : nxt;
  }

  async function drop(stage: string) {
    setOver(null);
    if (!drag || drag.target !== stage) return;      // inert lane; nothing to do
    const item = drag.item;
    setDrag(null);
    setMoving(item.action_id);
    try {
      const res = await api.advance(item.action_id);
      setNote({ tone: "good", text: `${item.pattern_name} → ${res.to}` +
        (res.wrote_adjustment_history ? " · written to adjustment_history" : "") });
      refresh();
    } catch (e) {
      // The card stays where it was, and the server's sentence is shown verbatim — it
      // names the role the stage actually needs, which is the thing the reader wants.
      setNote({ tone: "bad", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setMoving(null);
    }
  }

  const total = Object.values(board.counts).reduce((a, b) => a + b, 0);
  const draggableCount = board.order
    .flatMap((s) => board.lanes[s] ?? [])
    .filter((d) => legalTarget(d)).length;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-title font-semibold text-content-primary">Approval board</h1>
        <p className="mt-1 max-w-3xl text-label leading-relaxed text-content-muted">
          {total} item(s) across the chain. The agent may only write{" "}
          <span className="font-medium text-content-secondary">draft</span>; every arrow after it
          is a person. Executing writes{" "}
          <code className="font-mono">vrr_agent.adjustment_history</code>, which is what
          the ρ learning loop reads back.
        </p>
        {/* Drag is not discoverable on its own, so it is stated — and stated in terms of
            what THIS reader can move, since the answer depends entirely on their role. */}
        <p className="mt-1.5 text-micro text-content-muted">
          {!role ? (
            <>Sign in to act on the chain. Each lane advances on a different role's sign-off.</>
          ) : draggableCount > 0 ? (
            <>
              You are <strong className="text-content-secondary">{role}</strong> —{" "}
              {draggableCount} card(s) are yours to approve. Drag one to the next lane, or
              click it to read the case first. Only the next lane accepts a drop; the chain
              cannot be skipped.
            </>
          ) : (
            <>
              You are <strong className="text-content-secondary">{role}</strong> — nothing
              in the chain is waiting on you, so no card will lift.
            </>
          )}
        </p>
      </header>

      {note && <Banner tone={note.tone === "good" ? "good" : "bad"} title={note.text} />}

      {/* --------------------------------------------------------- swim lanes */}
      <div className="flex gap-3 overflow-x-auto pb-2">
        {board.order.map((stage) => {
          const items = board.lanes[stage] ?? [];
          const style = LANE_STYLE[stage] ?? LANE_STYLE.draft;
          const needed = board.approver_for_stage[stage];
          const mine = needed && role === needed;
          const isTarget = drag?.target === stage;
          return (
            <section key={stage} className="flex w-64 shrink-0 flex-col"
              // Only the one legal lane reacts. preventDefault on dragOver is what marks
              // an element as a drop target at all, so withholding it on every other lane
              // means the browser itself shows "no drop" there — the rule is enforced by
              // the platform rather than by a check after the fact.
              onDragOver={(e) => { if (isTarget) { e.preventDefault(); setOver(stage); } }}
              onDragLeave={() => setOver((o) => (o === stage ? null : o))}
              onDrop={() => drop(stage)}
            >
              <div className="mb-1.5 flex items-baseline gap-2">
                <span className={`h-2 w-2 rounded-full ${style.dot}`} />
                <h2 className={`text-label font-semibold uppercase tracking-wide ${style.head}`}>
                  {stage}
                </h2>
                <span className="text-label tabular-nums text-content-muted">{items.length}</span>
                {mine && (
                  <span className="ml-auto rounded bg-brand-50 px-1.5 py-0.5 text-micro font-medium text-brand-600">
                    yours
                  </span>
                )}
              </div>
              <p className="mb-2 h-8 text-micro leading-snug text-content-muted">
                {LANE_HINT[stage]}
              </p>

              <div className={`max-h-[calc(100vh-19rem)] flex-1 space-y-2 overflow-y-auto rounded-lg p-2 transition ${
                isTarget
                  ? over === stage
                    ? "bg-signal-soft ring-2 ring-signal"        // hovering the legal lane
                    : "bg-surface-raised/70 ring-2 ring-dashed ring-signal/50"
                  : "bg-surface-raised/70"}`}
              >
                {items.length === 0 && (
                  <p className="px-1 py-5 text-center text-micro text-content-muted">
                    {isTarget ? "drop to approve" : "empty"}
                  </p>
                )}
                {items.map((d) => {
                  const target = legalTarget(d);
                  const inFlight = moving === d.action_id;
                  return (
                    <button
                      key={d.action_id}
                      onClick={() => setOpen(d)}
                      // Draggable only when there is somewhere legal to drag to, so a
                      // card that cannot move does not pretend it can by lifting.
                      draggable={!!target && !inFlight}
                      onDragStart={(e) => {
                        if (!target) return;
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", d.action_id);
                        setDrag({ item: d, target });
                      }}
                      onDragEnd={() => { setDrag(null); setOver(null); }}
                      title={target
                        ? `Drag to ${target} to approve, or click to read it first`
                        : `Advances on ${board.approver_for_stage[d.stage] ?? "—"} sign-off`}
                      className={`w-full rounded-md bg-surface-card p-2.5 text-left shadow-card ring-1 transition hover:shadow-panel ${style.ring} ${
                        target ? "cursor-grab active:cursor-grabbing" : ""} ${
                        inFlight ? "opacity-50" : ""} ${
                        drag?.item.action_id === d.action_id ? "opacity-40" : ""}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-label font-semibold text-content-primary">
                          {/* A grip, shown only on cards that actually lift — the one
                              affordance that says "this is draggable" without a tooltip. */}
                          {target && <GripIcon />}
                          {d.pattern_name}
                        </span>
                        <Badge tone={d.severity === "high" ? "red" : "slate"}>
                          {d.severity}
                        </Badge>
                      </div>
                      <p className="mt-0.5 text-micro text-content-muted">
                        {fmt.month(String(d.vrr_date))} · {d.action_type.replace(/_/g, " ")}
                      </p>
                      {d.driver && (
                        <p className="mt-1 truncate font-mono text-micro text-content-muted">
                          {d.driver}
                        </p>
                      )}
                      {inFlight && (
                        <p className="mt-1 text-micro text-brand-600">approving…</p>
                      )}
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>

      {/* ------------------------------------------------------- detail panel */}
      {open && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/60"
             onClick={() => setOpen(null)}>
          <div className="h-full w-[min(34rem,100vw)] overflow-y-auto bg-surface-card p-5 shadow-panel"
               onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <h2 className="text-sub font-semibold text-content-primary">
                  {open.pattern_name} · {fmt.month(String(open.vrr_date))}
                </h2>
                <p className="mt-0.5 text-micro text-content-muted">
                  <code className="font-mono">{open.action_id}</code> · stage{" "}
                  <strong>{open.stage}</strong> · raised by {open.stage_by ?? "agent"}
                </p>
              </div>
              <button onClick={() => setOpen(null)}
                      className="rounded px-2 text-content-muted hover:bg-surface-raised">✕</button>
            </div>

            {open.narrative && (
              <pre className="mt-3 whitespace-pre-wrap rounded-md bg-surface-raised p-3 text-micro leading-relaxed text-content-secondary">
                {open.narrative}
              </pre>
            )}

            {(() => {
              const rec = typeof open.recommendation === "string"
                ? JSON.parse(open.recommendation) : open.recommendation;
              return rec?.injector_changes?.length ? (
                <div className="mt-3">
                  <p className="mb-1 text-label font-medium text-content-secondary">
                    Proposed injector changes
                  </p>
                  <DataTable rows={rec.injector_changes} max={240} />
                </div>
              ) : null;
            })()}

            {board.approver_for_stage[open.stage] === role ? (
              <div className="mt-4 flex gap-2">
                <button
                  onClick={() => act(open, "advance")}
                  className="rounded-md bg-signal px-3 py-1.5 text-body font-medium text-surface-base hover:brightness-110"
                >
                  Approve → {nextOf(board.order, open.stage)}
                </button>
                <button
                  onClick={() => act(open, "reject")}
                  className="rounded-md border border-surface-border px-3 py-1.5 text-body text-content-secondary hover:bg-surface-raised"
                >
                  Reject
                </button>
              </div>
            ) : (
              <p className="mt-4 rounded-md bg-surface-raised p-3 text-micro text-content-muted">
                Stage <strong>{open.stage}</strong> advances on{" "}
                <strong>{board.approver_for_stage[open.stage] ?? "—"}</strong> sign-off.
                {role
                  ? <> You are signed in as <strong>{role}</strong>.</>
                  : <> You are not signed in.</>}
              </p>
            )}
          </div>
        </div>
      )}

      <Card title="Executed adjustments"
            sub="The ρ-learning input: predicted vs actual post-VRR per executed change.">
        <DataTable rows={history as unknown as Record<string, unknown>[]} />
      </Card>
    </div>
  );
}

function nextOf(order: string[], stage: string): string {
  const i = order.indexOf(stage);
  return i >= 0 && i + 1 < order.length ? order[i + 1] : "—";
}

/** Six dots — the conventional "this lifts" mark. Inline SVG on currentColor, per the
 *  July audit's finding that emoji-as-icons are announced as their unicode names. */
function GripIcon() {
  return (
    <svg width="7" height="11" viewBox="0 0 6 10" fill="currentColor" aria-hidden
         className="mr-1 inline-block align-middle text-content-muted">
      <circle cx="1.2" cy="1.2" r="1.1" /><circle cx="4.8" cy="1.2" r="1.1" />
      <circle cx="1.2" cy="5" r="1.1" /><circle cx="4.8" cy="5" r="1.1" />
      <circle cx="1.2" cy="8.8" r="1.1" /><circle cx="4.8" cy="8.8" r="1.1" />
    </svg>
  );
}
