# Causeway architecture

This document describes the formats and algorithms in enough detail to reimplement them or to audit what a result means. The README covers how to use them.

- [Run layout on disk](#run-layout-on-disk)
- [Hashing](#hashing)
- [Event schema](#event-schema-causewayeventv1)
- [The graph and its evidence grades](#the-graph-and-its-evidence-grades)
- [Argument provenance and alerts](#argument-provenance-and-alerts)
- [Run replay](#run-replay)
- [Decision replay](#decision-replay)
- [Statistics](#statistics)
- [Server and ingestion](#server-and-ingestion)
- [Cross-run influence graph](#cross-run-influence-graph)

## Run layout on disk

```
runs/
  demo-seed0/
    events.jsonl          one event per line, hash-chained, append-only
    blobs/<sha256>.json   every value, stored once: {"v": <value>}
    tests.jsonl           results of counterfactual tests run against this run (optional)
```

Storage is plain files for now. Every reader goes through `causeway.core.load_run`, so swapping in a database means replacing that function and `Runtime._emit` / `Runtime._blob`.

## Hashing

| What | How |
|---|---|
| Canonical JSON | `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` |
| Blob ref | `"sha256:" + sha256(canonical({"v": value}))` |
| Event hash | `sha256(canonical(event without the "hash" key))`, hex |
| Chain | `event.prev_hash` = previous event's `hash`; 64 zeros for seq 0 |

Wrapping the value in `{"v": ...}` gives strings, numbers and objects one hashing rule. Because the event hash covers `prev_hash`, editing, deleting, inserting or reordering any event breaks every later link. `causeway verify` checks contiguous `seq`, every link, every event hash, every blob hash, and that every ref an event mentions exists.

The chain is unsigned. Anyone who can rewrite the whole file can rebuild a consistent chain. Tracekit solves that with a separate signer and an external witness, and the plan is to write Causeway events through it.

## Event schema (`causeway.event.v1`)

Every event has these fields:

| Field | Meaning |
|---|---|
| `schema` | `"causeway.event.v1"` |
| `seq` | 0, 1, 2, … within the run |
| `id` | random 128-bit hex id |
| `prev_hash`, `hash` | the chain |
| `ts` | UTC, microseconds, host clock (order comes from `seq`, not `ts`) |
| `run_id` | the run |
| `agent` | agent name, or `"run"` for run-level events |
| `type` | one of the types below |

Type-specific fields:

| Type | Fields |
|---|---|
| `run.start` | `program` (`module:ATTR` used for replay), `seed`, `interventions` (non-empty only in replays), `models`, `tools`, `sensitive_tools`, `meta` |
| `agent.start` | `parent`, `role` |
| `input` | `ref`, `source` (free label such as `web:vendor-notes`), `trust` (`trusted` / `untrusted`), `kind` (`task` or `input`) |
| `decision` | `model`, `purpose`, `params`, `seed`, `context` (list of `{ref, kind, source, trust, from_event}`), `ablated` (refs removed by an intervention), `output` (ref), `usage`, `status`, `duration_ms` |
| `action` | `tool`, `args_ref`, `result_ref`, `decision` (event id that asked for it, or null), `status` (`ok` / `error`), `mode` (`live` / `tape` / `stub`), `sensitive`, `duration_ms` |
| `message` | `to`, `ref`, `decision`, `trust` |
| `run.end` | `outcome` |

`context[].from_event` is the event that brought that value into the run. It is what turns "this value was in the context" into a precise edge, even when two events produced identical content.

Trust propagates at record time: a decision whose context holds any untrusted item produces an output marked untrusted, and messages and actions inherit it. This is a conservative label for display. The graph and the tests do the actual analysis.

## The graph and its evidence grades

Nodes are `input`, `decision`, `action` and `message` events. Edges:

| Edge kind | Grade | Rule |
|---|---|---|
| `context:<kind>` | observed | item X was in decision D's context (`from_event` → D) |
| `invoked` | observed | decision D asked for action A |
| `sent` | observed | decision D produced the message M |
| `same-content` | observed | an input has the exact hash of something produced earlier in the run, such as a file one agent wrote and another read |
| `content-overlap` | inferred | at least 3 shared word 5-grams and overlap ≥ 0.3 (`|A∩B| / min(|A|,|B|)`), with no observed path already linking the two |
| `causal` | tested | a saved test removed X and the target changed (or did not) |

Observed context edges also carry `reuse`: the share of the input's 5-grams that reappear in the decision's output. It hints at which inputs the model drew on. It is not evidence of cause.

Tested edges carry `effect`, `ci`, `n` and `verdict`. Edges with a CI that includes zero are kept as "ruled out" and are not followed when tracing.

**Taint / blast radius** is forward reachability over observed edges (optionally inferred) from chosen input nodes. It answers "what could this have influenced?", which is a superset of what it did influence.

## Argument provenance and alerts

For each tool call, `analysis.atoms` extracts values worth tracing: email addresses, URLs, and string arguments of 6+ characters. For each value, it searches earlier inputs and tool results for a case-insensitive verbatim match:

| Status | Meaning |
|---|---|
| `untrusted-only` | only untrusted content contained it |
| `trusted` | at least one trusted input or tool result contained it |
| `generated` | nothing earlier contained it; the model wrote it |

A tool result counts as untrusted when its content was also recorded as untrusted input (the Anthropic adapter does this for tools listed in `untrusted_tools`).

Alerts:

| Severity | Condition |
|---|---|
| high | sensitive action with at least one `untrusted-only` argument value; or the log fails verification |
| review | sensitive action downstream of an untrusted input (reach only); failed tool call; failed model call |
| info | tool call stubbed during a replay |

A tool is sensitive when the run declared it (`sensitive_tools`, or `act(..., sensitive=True)`). If a run declares none, names matching send, email, post, publish, transfer, pay, refund, delete, remove, export, upload, exec, shell, bash, write, deploy, sign or approve are treated as sensitive and labelled "by name".

## Run replay

`replay.counterfactual(run, intervention, target, n)`:

1. Load the `System` named in `run.start.program` (or use the one passed in).
2. Build a **tape** from the original run: a map from `(agent, tool, args hash, occurrence)` to the recorded result.
3. For i in 0..n-1, with seed `s_i` derived from the run id and i:
   - run the program with seed `s_i`, tape on, no intervention → did the target happen?
   - run it again with seed `s_i`, tape on, intervention on → did the target happen?
4. Report `p_with`, `p_without`, `effect = p_with - p_without`, the Newcombe 95% CI, pair flips, how many trials the intervention actually removed something in, and how many tool calls left the tape.

**Interventions** remove matching items from every decision's context at call time and record what was removed in `ablated`:

| Spec | Matches |
|---|---|
| `input:<glob>` | inputs whose source matches |
| `msg:<from>-><to>` | messages on that channel (globs allowed) |
| `result:<tool glob>` | tool results |
| `ref:<hash prefix>` | a specific content |
| `untrusted` | anything marked untrusted |

**Tape and stubs.** A tool call identical to a recorded one replays the recorded result. A call the original run never made is stubbed (`{"_unrecorded": true}`) unless `live_tools=True`. A replay therefore never repeats a real side effect. The cost is that behaviour after a stubbed call can differ from what the real tool would have caused, which is why `off_tape_actions` is reported.

**Paired seeds** (common random numbers) mean the only difference within a pair is the intervention, which cuts the variance of the estimated difference considerably.

**Verdicts:** `causal` if the CI lower bound > 0; `suppressive` if the upper bound < 0; `no-detectable-effect` otherwise; `not-applied` if the spec matched nothing in any trial.

What a `causal` verdict means: in this program, on this task, removing X changes how often the target happens. It is a total effect through every path. It says nothing about the model's internal reasons.

## Decision replay

`replay.decision_test(run, decision_seq, intervention, n, contains=None)` uses only the recorded log and the model function:

- rebuild the decision's context from blobs;
- call the model n times with the full context and n times without the matching items, paired seeds;
- with `contains`, measure P(output contains the text) in each arm and report the difference with a Newcombe CI;
- without it, measure how often the output differs from the paired full-context output, with a Wilson CI.

This measures the direct effect on one model call. It does not need the program to be re-runnable, which matters for production systems that can't be re-executed end to end. Decisions recorded through the Anthropic adapter can't be decision-replayed yet, because that needs a model function able to rebuild the original request.

## Statistics

- **Wilson score interval** for a single proportion.
- **Newcombe hybrid score interval** (method 10) for a difference of two proportions, built from the two Wilson intervals. It behaves well at 0% and 100%, which plain Wald intervals do not.

- **Exact McNemar test** on the discordant pairs (target happened only with the item, or only without it). Its p-value is reported as `p_value`. With b discordant pairs all in one direction the smallest possible p is 2 × 0.5^b, so fewer than 6 discordant pairs can never reach p < 0.05.
- **Benjamini–Hochberg** (`replay.benjamini_hochberg`) controls the false discovery rate when several inputs of one action are tested. The benchmark uses it at q = 0.05.

Intervals are computed at 95% (z = 1.96). The app's verdicts use each test's own interval and are not corrected across tests.

## Server and ingestion

`causeway serve ROOT` runs a threaded HTTP server.

| Method | Path | Notes |
|---|---|---|
| GET | `/` | the app, fetching from the API |
| GET | `/api/workspace` | run summaries and the cross-run influence graph |
| GET | `/api/runs/<id>` | everything the app shows for one run |
| POST | `/api/runs/<id>/tests` | `{"intervention", "target", "n"}` → run replay; only for programs passed with `--allow-program`; cross-origin requests refused |
| POST | `/v1/ingest` | `Authorization: Bearer <token>`; body `{"events": [...], "blobs": {ref: value}}` |
| GET | `/healthz` | liveness |

Ingest validation, all before anything is written:

1. Bearer token compared in constant time.
2. Every blob's content matches its ref.
3. All events in a batch share a run id, and the run id is a safe name.
4. Each event's `seq` is exactly one past the stored head, `prev_hash` equals the stored head hash, and the event hash recomputes.
5. Every ref an event mentions exists in the batch or on disk.

Rejections: 401 bad token, 409 gap / fork / replayed batch, 422 bad hash or missing blob, 413 body over 20 MB.

`HttpSink` batches events (default 50) and flushes on `Runtime.finish()`. It raises on HTTP errors, so a lost batch is visible to the caller.

**Security posture.** The app and read API have no authentication. The server binds to 127.0.0.1 by default and prints a warning for any other host. Replay imports Python code named in the run, which is why it is restricted to an explicit allow-list: an ingested run can name any module.

## Cross-run influence graph

`analysis.influence_graph(paths)`:

- **Sources** are input contents, keyed by blob hash, so the same document is one node across runs even under different labels.
- **Outcomes** are tool names.
- An edge (source, tool) counts the runs in which the source reached that tool (`reach_runs`) and collects every test in those runs whose intervention was `input:<that source>`.
- `cited_by` counts observed context edges from the source, meaning how many decisions used it.

A citation index counts who cited whom. This one also records which citations were shown to matter. Tests are attached to outcomes by the tool named in the target, so a test targeting `send_email,arg~vendor-compliance` shows on the `send_email` outcome. The tooltip keeps the full target.
