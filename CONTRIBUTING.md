# Contributing to Causeway

Thanks for helping. Causeway is early, so the most useful contributions right now are:

1. **Adapters** for agent frameworks and SDKs (OpenAI, LangGraph, OpenAI Agents SDK, CrewAI, AutoGen, LlamaIndex, MCP). See `causeway/adapters/anthropic.py` for the pattern: turn each request's context into content-addressed items and link tool results back to the decision that asked for them.
2. **Benchmark scenarios** in `bench/scenarios.py`, especially ones where today's methods get the cause wrong.
3. **Real-model benchmark runs.** Run `python -m bench --model claude ...` (or add a backend for another provider) and open a pull request with the files in `bench/results/`.
4. **Bug reports** with a recorded run attached (`causeway verify` output helps).

## Development setup

```bash
git clone https://github.com/Cygnux-Labs/Causeway && cd causeway
pip install -e ".[test]"
python -m pytest -q          # must pass on Python 3.9 to 3.13
python -m bench              # about 6 seconds on the simulated model
```

## Ground rules

- **No new runtime dependencies** in `causeway/` without discussion. The core is standard library only, which is part of why it is easy to audit and self-host. Adapters may import their SDK lazily.
- **Every behaviour change needs a test.** Integrity and replay-safety changes need a test that fails without the fix.
- **Keep claims honest.** If a feature has only been tested against a fake or simulated model, say so in the docstring and the README.
- **Replay must stay side-effect free by default.** Anything that could re-send a real tool call must be opt-in.
- **Event schema changes** (`causeway.event.v1`) need a new schema version and a migration note in `docs/architecture.md`.
- Write plain, direct prose in docs and comments.

## Pull requests

Keep them focused. Describe what changed, why, and how you tested it. CI runs the test suite on Python 3.9 to 3.13 and the demo end to end.

## Sign-off (DCO)

Sign your commits to certify the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "Add LangGraph adapter"
```

By contributing you agree that your contribution is licensed under the Apache License 2.0.

## Conduct

Be respectful and assume good faith. Harassment or personal attacks are not tolerated; maintainers may remove comments or contributors that break this. Report problems privately to the maintainers through a GitHub security advisory or the contact on the Cygnux Labs profile.
