# Changelog

## Unreleased

- **Live guard** (`causeway.guard.Guard`): checks sensitive tool calls before they run; `alert` or `block` mode, custom rules, `webhook()` notifier. Blocked calls are recorded with `status: "blocked"` and the verdict. Anthropic adapter: `TracedMessages.run_tool()` and `check()`.
- **Live app**: `causeway serve` watches the runs folder, pushes run changes and new alerts over `/api/stream`, shows runs in progress, and POSTs high alerts to `--webhook`. `causeway watch ROOT` for the terminal. `HttpSink` sends after every tool call and every `interval` seconds.
- **Tracekit import**: `causeway import tracekit` turns a Tracekit run into a Causeway run with exact model-call context, cross-agent messages, blocked calls, evidence alerts and a `tracekit-map.json` citing the signed records; verifies signatures when `tracekit` is installed.
- **Decision replay for real models**: the adapter records each request's layout; `adapters.anthropic.replay_model()` and `causeway test --decision ... --anthropic` re-send a call without an input. Decision tests without `--contains` now subtract a no-removal control, so sampling noise no longer reads as an effect.
- **Find the cause** (`attribute()`, `causeway attribute`, a button in the app, `POST /api/runs/<id>/attribute`): remove all suspects at once, then narrow down; one shared baseline arm across tests (`base_cache`); primary causes, contributing factors and joint (redundant) causes judged on intervals; says when the agent acts on its own. 6–20% fewer replay calls on the simulated benchmark, and one test instead of one per input when nothing is the cause. `--workers` for parallel replays.
- **Verdicts you can trust**: a test says `ruled-out` only when the interval bounds the effect below 20 points (`min_effect`), and `inconclusive` (with a suggested `n`) when it can't tell. It used to call any interval that included zero "no detectable effect", and the app showed that as "ruled out".
- **Sequential replay tests** (`--n-max`, `counterfactual(..., n_max=)`, `decision_test(..., n_max=)`): double the trials until the verdict is decisive, with Bonferroni across looks. Prompted by the first real-model benchmark, where a 7B model repeated harmful actions in only some replays.
- **First real-model benchmark** (Qwen 2.5 7B, local): with sequential tests, Causeway named exactly the planted cause in 6 of 6 harmful runs where a ground-truth check confirms it drives the harm, with no false blames; results in `bench/results/qwen2.5_7b-2026-10-09-*`. The first run (10 fixed replays: 1 of 10) drove the fixes above and below.
- Benchmark: `--oracle M` ground-truth check (fresh runs with and without the planted document); scores Causeway's own verdict, corrected for the number of inputs tested (`family=`); futility stopping for inputs with no effect.
- Benchmark: `openai` backend for any OpenAI-compatible endpoint (local Ollama by default, no key), JSON-constrained output, raw replies kept in the log, unparseable replies counted instead of read as "no action" (a parser bug had hidden harmful runs), `--n-max`.
- `Runtime(untrusted_tools=...)` and `act(..., untrusted=True)` record tool results as untrusted content.
- Fixes: the runtime is thread-safe (concurrent agents broke the hash chain); a value from the trusted task no longer counts as "untrusted-only"; `{"error": None}` is not a failure; the server refuses non-loopback `Host` headers on a loopback bind, answers 400/500 instead of dropping connections, validates ingested event fields, and skips a malformed run instead of failing the workspace.

## 0.3.0 (2026-10-06)

- Licensed under Apache-2.0. PyPI distribution name `causeway-ai` (import name unchanged: `causeway`).
- Attribution benchmark (`bench/`): five scenarios with a planted cause plus a control, four methods (reach, provenance, reuse, replay), simulated and Claude backends, Markdown and JSON results.
- Counterfactual tests report an exact McNemar p-value; `benjamini_hochberg` for false-discovery-rate control.
- CONTRIBUTING, SECURITY, NOTICE, issue and PR templates, PyPI release workflow with trusted publishing.
- README: benchmark and related-work sections.

## 0.2.0 (2026-10-06)

- Investigation app: runs list, overview, timeline, "why did it happen?", graph, agents, cross-run influence. Works as a static report or served live.
- Alerts: untrusted-only argument values on sensitive tools (high), sensitive actions downstream of untrusted input (review), failures.
- Argument provenance for every tool call.
- Decision replay: re-call one recorded model call with and without a context item, from the log alone.
- `causeway serve`: JSON API, "Run test" from the UI (allow-listed programs only), token-protected `/v1/ingest` with chain and blob validation.
- `HttpSink` for remote agents.
- Anthropic Messages adapter with automatic context capture.
- SDK records latency, token usage, status and sensitivity; `record_decision` / `record_action` for calls made outside the runtime.
- Examples, architecture doc, CI on Python 3.9 to 3.13.

## 0.1.0 (2026-10-05)

- Content-addressed, hash-chained causal event log and runtime.
- Graph with observed / inferred / tested edges, taint queries.
- Run replay with tape and stubs, paired-seed counterfactual tests with Newcombe CIs.
- Multi-agent structure metrics, influence matrix, cross-run influence index.
- Three-agent demo with a poisoned vendor note.
