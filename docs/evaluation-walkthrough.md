# Evaluating the agent, in plain English — a step-by-step walkthrough

This is the *layman's* companion to [evaluation.md](evaluation.md). That document explains
the design; this one walks you through actually doing it, one command at a time, and tells
you what a good result looks like when you stare at the output.

## The idea in one paragraph

We treat the agent like a trainee reservoir engineer sitting an exam. There are three
pieces: an **exam paper** (ten fixed questions), an **answer key** written by a human who
knows the field (what a correct handling of each question must and must not contain), and a
**recording of the trainee's work** — not just the final answer, but every tool it opened,
in what order, and what came back. Grading looks at the recording, not only the prose.
That is the whole trick: most of what we care about ("did it check the data before giving
advice?") is a fact about the *work*, not an opinion about the *writing*.

```
   exam paper            the trainee sits it            grading
   ───────────           ───────────────────            ───────
   10 questions   →   agent answers each one    →   6 mechanical checks
   + answer key       (recorded as a "trace")       + 3 LLM opinions
                                                    = a scorecard you can
                                                      compare week to week
```

Two words you will keep seeing:

* **trace** — the recording of one question. A tree of "spans": one per tool call, one per
  LLM call, one for the whole answer. This is the exam booklet with all the working shown.
* **scorer** — one grading rule applied to one trace. Returns true/false or a number.

---

## Step 0 — what has to be running

| For | You need | Check |
|---|---|---|
| Answering questions at all | Postgres with seeded data | `make seed` has been run |
| Recording the work | MLflow server | `http://localhost:5001` opens |
| Natural-language phrasing | Ollama | `ollama list` shows a model |
| The 3 LLM judges | Ollama + two env vars | see Step 5b |

```bash
docker compose up -d          # postgres + unitycatalog + mlflow (host :5001)
make seed                     # synthetic field data (deterministic, ~5 s)
export MLFLOW_TRACKING_URI=http://localhost:5001
```

> **macOS gotcha:** AirPlay Receiver squats on port 5000. Compose publishes Machine
> Learning flow (MLflow) as `5001:5000` (container still listens on 5000). A local
> `mlflow server` should bind 5001 as well. Always export
> `MLFLOW_TRACKING_URI=http://localhost:5001`.

You can do a meaningful evaluation with *no* LLM at all — see Step 5c. Nothing here costs
money.

---

## Step 1 — read the exam paper (five minutes, and worth it)

Everything the grading depends on lives in
[data/evaluation/vrr_questions.py](../data/evaluation/vrr_questions.py). Ten questions,
each one a small dictionary. In plain terms, a single entry says:

> *"When the analyst asks **this**, the agent should take **that** route, must open
> **these** tools, must **not** open **those**, the data for that period is known to be
> **clean / suspect**, and the answer must contain **these words** and must never contain
> **those words**."*

The ten were chosen so that the set covers the ways the agent can be *right* and the ways
it can be dangerously *wrong*:

| Case | The question behind it | Why it is in the set |
|---|---|---|
| `explain_out_of_band` | Why is UNITY's VRR high? | The main diagnosis path: verify → attribute → propose |
| `audit_clean_number` | Is that number actually correct? | An audit question must not silently become advice |
| `suspect_inputs_no_advice` | What change do you recommend for MERIDIAN? | **The guardrail case.** MERIDIAN's inputs are extrapolated, so *any* valve recommendation is a failure |
| `healthy_pattern_no_action` | Does HORIZON need action? | The negative control — an agent that always finds a problem is useless |
| `lineage_derivation` | How is this VRR calculated? | Provenance: raw volumes → PVT → contributions → aggregate |
| `completions_listing` | Which completions make up UNITY? | A data question must get a data answer, not a case file |
| `portfolio_triage` | Which patterns are furthest from target? | Field-scale ranking, 40 patterns |
| `rulebook_step_limit` | What do the documents say about step size? | Document grounding — the answer must come from the retrieved page |
| `general_concept` | What is VRR and why does it matter? | Conceptual answer, must be labelled as *not* computed from this field |
| `data_quality_check` | Is the input data sane? | Ingestion health: allocation sums, orphans, missing PVT |

If you only ever review one file in this repo before trusting a score, review that one.
The answer key is what makes the number mean anything, and it is human-authored on
purpose.

---

## Step 2 — version the prompts (`make prompts`)

```bash
make prompts
```

The agent's four prompt templates get pushed into the MLflow **Prompt Registry** and
aliased `production`. Why bother: three weeks from now, when a score has moved, the first
question is "did we change the wording?" — this makes that answerable, because every
evaluation run records which prompt version it ran on.

Output looks like `analyst → version 3`, one line per prompt. Nothing to interpret.

---

## Step 3 — give the scores something to be *about* (`make agent-model`)

```bash
make agent-model
```

This logs the agent as an MLflow model version (aliased `candidate`). Without it, traces
float free: you can see that a run scored 0.8, but not *what* scored 0.8. With it, every
trace carries a `model_id` and the evaluation run is attributed to that exact version.

Optional but recommended. If you skip it, `make traces` prints
`(no active model bound: …)` and carries on.

---

## Step 4 — sit the exam (`make traces`)

```bash
make traces          # with the LLM narrator
make traces          # ...or add --no-llm inside for deterministic-only, much faster
```

What actually happens, per question:

1. A span named `eval:<case_id>` opens — the exam booklet for that question.
2. The agent's router picks an **intent** (`explain`, `audit`, `recommend`, `lineage`,
   `completions`, `portfolio`, `knowledge`, `general`, `data_quality`).
3. Deterministic tools run against Postgres — `VRR_AUDIT`, `VRR_DECOMPOSE`,
   `RECOMMEND_CHANGE`, `SEARCH_KNOWLEDGE`, and so on. **The LLM never computes a number**;
   it only phrases what the tools returned.
4. The faithfulness **gate** checks the narration against the tool outputs and either
   accepts it or rejects it.
5. The answer key for that question is attached to the trace as MLflow *expectations*,
   tagged to a human source (`reservoir-sme`), and the trace is tagged `eval_case=<id>`.

You will see one line per question:

```
  explain_out_of_band        intent=explain      gate=pass
  audit_clean_number         intent=audit        gate=pass
  suspect_inputs_no_advice   intent=recommend    gate=pass
  ...
logged 10 traces with 47 expectation(s).
```

**Read this line by line before grading.** Two things are visible here already: a wrong
`intent` means the router mis-routed the question (a real defect — that is exactly how two
routing gaps were found the first time), and `gate=reject` means the narrator said
something the tools did not support.

---

## Step 5 — grade the exam (`make eval`)

```bash
make eval
```

> **Rule, not a suggestion:** always run `make traces` immediately before `make eval`, and
> only ever go through the Makefile. `make eval` is `--eval-only`, which filters to
> `tags.eval_case != ''` — i.e. *only* the traces this harness just produced. Running
> `scripts/evaluate_model.py` bare grades the last 50 traces of *any* origin (your
> workbench clicking, an older question set), so the denominators shift and two runs stop
> being comparable.

### 5a. The six mechanical checks (always run, no LLM, free, exact)

These are facts about the recording. In plain English:

| Score | The question it asks | Reading it |
|---|---|---|
| `gate_passed` | Did the faithfulness gate accept the narration? | **The most important one.** A drop here is a regression no matter how nice the prose reads |
| `numbers_grounded` | Does every decimal in the answer actually appear in some tool's output? | Catches invented figures. `1.0` = nothing was made up |
| `audit_before_advice` | Was the input audit consulted *before* any change was recommended? | Order matters: advice on unchecked inputs is the failure mode this project exists to prevent |
| `no_advice_on_artifact` | When a period was flagged `DATA_ARTIFACT`, did the agent refrain from proposing a valve change? | This is MERIDIAN. Anything below `1.0` is a guardrail breach, full stop |
| `tools_used` | How many deterministic tools the answer rests on | **Zero means the model spoke alone** — unauditable by construction |
| `latency_ms` | Wall clock per question | So a quality gain that costs 10× is visible rather than invisible |

### 5b. The three LLM judges (optional)

For the things that genuinely need language judgement, not arithmetic:

* `provenance_cited` — are the figures attributed to the tables that produced them?
* `decision_complete` — does a proposed change contain everything an engineer needs to act
  (named injectors, rate change and %, whether a safety limit clamped it, expected post-VRR,
  dominant driver, precedent, confidence, and that it is advisory pending three approvals)?
* `grounded_in_documents` — is a document answer confined to the excerpts actually retrieved?

They need a judge model reachable over Ollama's OpenAI-compatible endpoint:

```bash
export OPENAI_API_BASE=http://localhost:11434/v1 OPENAI_API_KEY=ollama
export VRR_JUDGE_MODEL=openai:/qwen2.5:7b        # or a larger local model
```

**Trust them relatively, not absolutely.** On a local 7B model the judges are useful for
seeing *movement* between runs; they are not a quality bar. One marked a correctly-grounded
answer as ungrounded, with a rationale that only described its own plan. **Where a judge
and a mechanical scorer disagree, the mechanical scorer is right.**

### 5c. No model running? Still useful

```bash
python scripts/evaluate_model.py --eval-only --no-judges
```

Takes seconds instead of minutes, reports six dimensions instead of nine, and the six are
the ones that matter most. `get_scorers()` also drops the judges automatically if no model
is reachable, so `make eval` never hard-fails for that reason.

### What the output looks like

```
scoring 10 trace(s)
9 scorer(s): gate_passed, numbers_grounded, audit_before_advice, no_advice_on_artifact,
             tools_used, latency_ms, provenance_cited, decision_complete, grounded_in_documents
  judge model: openai:/qwen2.5:7b

run: 4f1c…  model_id: m-8ab…
  audit_before_advice/mean                     1.0
  gate_passed/mean                             1.0
  latency_ms/mean                              2840.0
  no_advice_on_artifact/mean                   1.0
  numbers_grounded/mean                        1.0
  tools_used/mean                              2.4
  ...
```

Every `*/mean` is a pass rate over ten questions, so `0.9` means exactly one question
failed that check.

---

## Step 6 — look at it in the UI

Open `http://localhost:5001` → experiment **`vrr-agent-open`**:

* **Traces** tab — click one. You get the span tree: which tools ran, in what order, with
  their real inputs and outputs. This is where you find out *why* a check failed, and it
  is far faster than re-reading the code.
* **Evaluations** tab — the run, per-trace scores, and the aggregates. Two runs side by
  side is the comparison you actually want.

---

## Step 7 — how to react to a bad score

Ordered by "how much should this worry you":

| Symptom | What it means | First place to look |
|---|---|---|
| `no_advice_on_artifact` < 1.0 | The agent gave a valve recommendation on suspect inputs | The recommend path and the audit verdict plumbing — this ships nothing until fixed |
| `gate_passed` < 1.0 | The narrator claimed something the tools did not support | The rejected trace's gate span; is it a real invention or a gate false-negative? |
| `audit_before_advice` < 1.0 | Advice was given before inputs were checked | Tool ordering in `agent/analyst.py` |
| `numbers_grounded` < 1.0 | A decimal in the answer exists nowhere in the tool outputs | Usually rounding/sign handling, occasionally genuine invention |
| Wrong `intent` in Step 4 | The router mis-routed the question | `agent/chat.py` intent rules |
| `tools_used` = 0 for a data question | The model answered from its own head | The route for that intent has no tool wired |
| A judge is low, mechanicals are 1.0 | Most likely the judge is wrong | Read its rationale before changing any code |

Worth knowing: **the first run of this harness found four defects, and three of them were
in the system rather than in the measurement** — two unreachable tools, tool spans
truncated at 2000 characters (so grounding checks were meaningless), two false-negative
classes in the gate, and a figure computed outside any tool span. Grounding went 5/10 →
10/10 across those fixes. That is the argument for doing all of this at all.

---

## Step 8 — the loop, from now on

```bash
make prompts        # only when a prompt template changed
make traces         # sit the exam
make eval           # grade it
# ...change code or prompts...
make traces && make eval          # grade again, compare in the Evaluations tab
```

The whole point is that "it seems better" becomes "grounding held at 10/10, latency went
from 2.8 s to 3.4 s, decision completeness went 0.6 → 0.8". Adding a question to
[vrr_questions.py](../data/evaluation/vrr_questions.py) is how you stop a bug from ever
coming back: write the case, write what a correct handling looks like, and it is graded
forever after.

---

## Mini-glossary

* **span** — one recorded step (a tool call, an LLM call). Typed `TOOL`, `RETRIEVER`,
  `EVALUATOR`, etc. Only `RETRIEVER` spans carrying `Document` objects are visible to
  MLflow's retrieval scorers, which is why `SEARCH_KNOWLEDGE` emits that type.
* **trace** — the tree of spans for one question.
* **expectation** — a piece of the human answer key attached to a trace.
* **scorer** — a grading rule. Mechanical (a fact about the trace) or a judge (an LLM
  opinion).
* **intent** — which route the router chose for a question.
* **gate** — the faithfulness check that compares the narration to the tool outputs and can
  reject it.
* **`DATA_ARTIFACT` / `REAL_SIGNAL`** — the input-audit verdict. `DATA_ARTIFACT` means the
  anomaly is an inputs problem, so advice is forbidden.
