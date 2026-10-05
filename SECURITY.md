# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through
[GitHub security advisories](https://github.com/Cygnux-Labs/Causeway/security/advisories/new)
rather than a public issue. Include the Causeway version (`python -c "import causeway; print(causeway.__version__)"`),
your platform, and steps to reproduce. You can expect an acknowledgement within a few days.

## What Causeway does and does not protect

- **The log detects edits, not a full rewrite.** Every event is hash-chained and every value is stored under its content hash, so `causeway verify` catches edited, deleted, reordered or inserted events and changed values. Anyone who can rewrite the whole log can rebuild a consistent chain. Signing and an external witness are planned through [Tracekit](https://github.com/Cygnux-Labs/Tracekit).
- **The app and read API have no authentication.** `causeway serve` binds to 127.0.0.1 by default and warns on any other host. Do not expose it to an untrusted network.
- **Ingestion** requires a bearer token and validates hashes, chain continuity and blob integrity before writing. A stolen token lets an attacker add runs.
- **Replay executes code.** It imports the program a run names. The server only replays programs passed with `--allow-program`. Run replays only for code you trust.
- **Replay does not repeat real side effects by default.** Tool calls not in the recording are stubbed unless `live_tools=True` / `--live-tools`.
- **Logs contain whatever your agents saw**, including prompts, documents and tool output. There is no redaction yet; treat run folders as sensitive data.

In scope: bypasses of verification, ingestion validation, the replay allow-list, the stub-by-default guarantee, path handling in the server, and cross-site issues in the app. Out of scope: the documented lack of app authentication and an attacker who can rewrite the whole log.

## Supported versions

Only the latest release receives fixes.
