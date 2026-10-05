# Changelog

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
