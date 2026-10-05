"""Record Anthropic Messages API calls without restructuring your agent.

    from anthropic import Anthropic
    from causeway import Runtime
    from causeway.adapters.anthropic import TracedMessages

    rt = Runtime({}, out_dir="runs", task="Resolve ticket 881")
    msgs = TracedMessages(Anthropic().messages, rt.agent("support"), untrusted_tools={"web_fetch"})
    resp = msgs.create(model=..., max_tokens=..., messages=[...], tools=[...])
    for block in resp.content:
        if block.type == "tool_use":
            result = run_my_tool(block.name, block.input)
            msgs.tool_result(block.id, result)          # records the action, linked to the decision
    ...
    rt.finish()

Every system prompt, message block and tool result the request carries becomes a content-addressed
context item, so the graph gets observed edges with no manual `decide([...])` lists. A tool_result
block is linked back to the action recorded with `tool_result()`; results of tools listed in
`untrusted_tools` are also marked untrusted so taint and alerts see them.

Not covered yet: streaming, and replaying adapter-recorded calls (decision_test needs a model
function that can rebuild the request; planned).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..core import Agent, Ref, content_hash


def _plain(obj: Any) -> Any:
    """SDK objects -> plain JSON-able data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


class TracedMessages:
    def __init__(self, messages_api: Any, agent: Agent, *, user_trust: str = "untrusted",
                 untrusted_tools: Iterable[str] = (), sensitive_tools: Iterable[str] = (),
                 purpose: str = "", trust_fn: Optional[Callable[[str, Any], Optional[str]]] = None):
        self._api = messages_api
        self.agent = agent
        self.user_trust = user_trust
        self.untrusted_tools = set(untrusted_tools)
        self.sensitive_tools = set(sensitive_tools)
        self.purpose = purpose
        self.trust_fn = trust_fn
        self._known: Dict[str, Ref] = {}           # content hash -> Ref already in the run
        self._tool_uses: Dict[str, Tuple[str, Any, Ref]] = {}  # tool_use_id -> (name, input, decision)
        self._results: Dict[str, Ref] = {}         # tool_use_id -> Ref used for its tool_result block

    # -- context building
    def _ref(self, value: Any, source: str, trust: str) -> Ref:
        h = content_hash(value)
        if h in self._known:
            return self._known[h]
        if self.trust_fn:
            trust = self.trust_fn(source, value) or trust
        r = self.agent.observe(value, source=source, trust=trust)
        self._known[h] = r
        return r

    def _context(self, kw: Dict[str, Any]) -> List[Ref]:
        ctx: List[Ref] = []
        if kw.get("system"):
            ctx.append(self._ref(_plain(kw["system"]), "system", "trusted"))
        if kw.get("tools"):
            ctx.append(self._ref(_plain(kw["tools"]), "tools", "trusted"))
        for i, m in enumerate(kw.get("messages", [])):
            m = _plain(m)
            role, content = m.get("role"), m.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for j, b in enumerate(blocks):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in self._results:
                    ctx.append(self._results[b["tool_use_id"]])
                elif role == "assistant":
                    ctx.append(self._ref(b, f"assistant#{i}.{j}", "trusted"))
                elif b.get("type") == "tool_result":
                    ctx.append(self._ref(b.get("content"), f"tool_result#{i}.{j}", self.user_trust))
                else:
                    ctx.append(self._ref(b, f"user#{i}.{j}", self.user_trust))
        return ctx

    # -- API
    def create(self, **kw: Any) -> Any:
        ctx = self._context(kw)
        t0 = time.perf_counter()
        try:
            resp = self._api.create(**kw)
        except Exception as e:
            self.agent.record_decision(ctx, {"error": f"{type(e).__name__}: {e}"}, model=str(kw.get("model")),
                                       purpose=self.purpose, status="error",
                                       duration_ms=(time.perf_counter() - t0) * 1000)
            raise
        dur = (time.perf_counter() - t0) * 1000
        content = _plain(getattr(resp, "content", []))
        usage = _plain(getattr(resp, "usage", None)) or {}
        params = {k: kw[k] for k in ("max_tokens", "temperature", "top_p", "top_k", "tool_choice") if k in kw}
        dec = self.agent.record_decision(ctx, {"content": content, "stop_reason": getattr(resp, "stop_reason", None)},
                                         model=str(kw.get("model")), purpose=self.purpose, params=params,
                                         usage={k: usage.get(k) for k in ("input_tokens", "output_tokens")
                                                if usage.get(k) is not None}, duration_ms=dur)
        for j, b in enumerate(content):
            # each assistant block, when sent back in the next request, should link to this decision
            ref = self.agent.rt._blob(b)  # stored, so the next request's context can point at it
            self._known[ref] = Ref(ref, b, "output", self.agent.name, self.purpose, dec.trust, dec.event_id)
            if b.get("type") == "tool_use":
                self._tool_uses[b["id"]] = (b["name"], b.get("input", {}), dec)
        return resp

    def tool_result(self, tool_use_id: str, result: Any, *, duration_ms: Optional[float] = None,
                    sensitive: Optional[bool] = None) -> Ref:
        """Record the tool call your code executed for a tool_use block."""
        if tool_use_id not in self._tool_uses:
            raise KeyError(f"unknown tool_use_id {tool_use_id!r}")
        name, args, dec = self._tool_uses[tool_use_id]
        act = self.agent.record_action(name, args if isinstance(args, dict) else {"input": args}, result,
                                       decision=dec, duration_ms=duration_ms,
                                       sensitive=sensitive if sensitive is not None else
                                       (name in self.sensitive_tools or None))
        ref = act
        if name in self.untrusted_tools:
            # same content -> the graph links action -> input with a same-content edge
            ref = self.agent.observe(result, source=f"tool:{name}", trust="untrusted")
        self._results[tool_use_id] = ref
        self._known[content_hash(result)] = ref
        return ref
