"""Causeway core: content-addressed refs, hash-chained causal events, and the agent runtime.

Every value an agent sees or produces is stored once under its content hash (a *ref*).
A decision records the exact refs that were in its context and the ref it produced, so
"X was in the context of D" becomes an edge X -> D with no guessing. Actions, results
and messages are linked the same way. The event log is hash-chained (unsigned here;
Tracekit can sign and witness it).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

SCHEMA = "causeway.event.v1"
GENESIS = "0" * 64

TRUST_LEVELS = ("trusted", "untrusted")
EVENT_TYPES = ("run.start", "agent.start", "input", "decision", "action", "message", "run.end")


# --------------------------------------------------------------------------- hashing

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical({"v": value})).hexdigest()


def event_hash(ev: Dict[str, Any]) -> str:
    body = {k: v for k, v in ev.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def _now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%06dZ" % int((t % 1) * 1e6)


def derive_seed(*parts: Any) -> int:
    return int(hashlib.sha256(canonical(list(parts))).hexdigest()[:12], 16)


# --------------------------------------------------------------------------- refs

@dataclass(frozen=True)
class Ref:
    """A value plus where it came from. Identity (for graph edges) is `ref`, the content hash."""

    ref: str
    value: Any
    kind: str  # task | input | output | message | result
    agent: str
    source: str = ""  # input: source label; message: "from->to"; result: tool name; output: purpose
    trust: str = "trusted"
    event_id: str = ""

    def __repr__(self) -> str:  # keep reprs short in logs
        return f"Ref({self.kind}:{self.source or self.agent} {self.ref[:15]})"


def ablation_matches(spec: str, r: Ref) -> bool:
    """Intervention specs:
    ref:<hash prefix>        a specific content (with or without the sha256: prefix)
    input:<glob>             inputs whose source matches the glob
    msg:<from>-><to>         messages on a channel (globs allowed, e.g. msg:*->planner)
    result:<tool glob>       tool results
    untrusted                anything marked untrusted
    """
    if spec == "untrusted":
        return r.trust == "untrusted"
    kind, _, pat = spec.partition(":")
    if kind == "ref":
        pat = pat if pat.startswith("sha256:") else "sha256:" + pat
        return r.ref.startswith(pat)
    if kind == "input":
        return r.kind == "input" and fnmatch.fnmatch(r.source, pat)
    if kind == "msg":
        return r.kind == "message" and fnmatch.fnmatch(r.source, pat)
    if kind == "result":
        return r.kind == "result" and fnmatch.fnmatch(r.source, pat)
    raise ValueError(f"unknown intervention spec: {spec!r}")


# --------------------------------------------------------------------------- run (loaded log)

@dataclass
class Run:
    run_id: str
    events: List[Dict[str, Any]]
    blobs: Dict[str, Any]
    path: Optional[str] = None
    tests: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def start(self) -> Dict[str, Any]:
        return self.events[0] if self.events and self.events[0]["type"] == "run.start" else {}

    def of_type(self, t: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["type"] == t]

    def by_id(self) -> Dict[str, Dict[str, Any]]:
        return {e["id"]: e for e in self.events}

    def value(self, ref: Optional[str]) -> Any:
        return self.blobs.get(ref) if ref else None

    def add_test(self, result: Dict[str, Any]) -> None:
        self.tests.append(result)
        if self.path:
            with open(os.path.join(self.path, "tests.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(result, sort_keys=True) + "\n")


def load_run(path: str) -> Run:
    events = []
    with open(os.path.join(path, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    blobs: Dict[str, Any] = {}
    bdir = os.path.join(path, "blobs")
    if os.path.isdir(bdir):
        for name in os.listdir(bdir):
            with open(os.path.join(bdir, name), encoding="utf-8") as f:
                blobs["sha256:" + name[:-5]] = json.load(f)["v"]
    tests = []
    tpath = os.path.join(path, "tests.jsonl")
    if os.path.exists(tpath):
        with open(tpath, encoding="utf-8") as f:
            tests = [json.loads(l) for l in f if l.strip()]
    run_id = events[0]["run_id"] if events else os.path.basename(path)
    return Run(run_id=run_id, events=events, blobs=blobs, path=path, tests=tests)


def verify(run: Run) -> List[str]:
    """Integrity check: chain links, event hashes, contiguous seq, blob content hashes,
    and that every referenced ref exists. Returns a list of problems (empty = ok)."""
    problems: List[str] = []
    prev = GENESIS
    for i, ev in enumerate(run.events):
        if ev.get("seq") != i:
            problems.append(f"seq {ev.get('seq')} at position {i}: not contiguous")
        if ev.get("prev_hash") != prev:
            problems.append(f"seq {i}: prev_hash does not link (record deleted, reordered or inserted)")
        if event_hash(ev) != ev.get("hash"):
            problems.append(f"seq {i}: hash mismatch (event content was edited)")
        prev = ev.get("hash", "")
    for ref, val in run.blobs.items():
        if content_hash(val) != ref:
            problems.append(f"blob {ref[:20]}: content does not match its hash")
    for ev in run.events:
        for r in referenced_refs(ev):
            if r not in run.blobs:
                problems.append(f"seq {ev['seq']}: references missing blob {r[:20]}")
    return problems


def referenced_refs(ev: Dict[str, Any]) -> List[str]:
    out = []
    for k in ("ref", "output", "args_ref", "result_ref"):
        if ev.get(k):
            out.append(ev[k])
    out.extend(c["ref"] for c in ev.get("context", []))
    return out


# --------------------------------------------------------------------------- sinks

class HttpSink:
    """Ships events and their blobs to a `causeway serve` collector (POST /v1/ingest).
    Batches; call flush() (Runtime.finish does) to send the rest. Raises on HTTP errors so a
    lost batch is visible to the caller rather than silently dropped."""

    def __init__(self, url: str, token: str, batch: int = 50, timeout: float = 10.0):
        self.url = url.rstrip("/") + "/v1/ingest"
        self.token = token
        self.batch = batch
        self.timeout = timeout
        self._events: List[Dict[str, Any]] = []
        self._blobs: Dict[str, Any] = {}

    def write(self, ev: Dict[str, Any], blobs: Dict[str, Any]) -> None:
        self._events.append(ev)
        self._blobs.update(blobs)
        if len(self._events) >= self.batch:
            self.flush()

    def flush(self) -> None:
        if not self._events:
            return
        body = json.dumps({"events": self._events, "blobs": self._blobs}, ensure_ascii=False, default=str).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status >= 300:
                raise RuntimeError(f"ingest failed: HTTP {r.status}")
        self._events, self._blobs = [], {}


@dataclass
class ModelOutput:
    """Optional richer return type for model functions: the value plus usage (tokens, cost)."""
    value: Any
    usage: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- runtime

ModelFn = Callable[..., Any]   # fn(context_values, *, purpose, seed, params, agent) -> value
ToolFn = Callable[..., Any]    # fn(**args) -> value


class Tape:
    """Recorded tool results from an earlier run, replayed VCR-style.
    Keyed by (agent, tool, args hash, occurrence) so repeated identical calls replay in order."""

    def __init__(self, run: Optional[Run] = None):
        self._results: Dict[tuple, Any] = {}
        if run is not None:
            seen: Dict[tuple, int] = {}
            for ev in run.of_type("action"):
                key = (ev["agent"], ev["tool"], ev["args_ref"])
                n = seen.get(key, 0)
                seen[key] = n + 1
                self._results[key + (n,)] = run.value(ev["result_ref"])

    def get(self, key: tuple):
        return self._results.get(key, _MISSING)


_MISSING = object()


class Runtime:
    """Records a multi-agent run. The same class runs counterfactual replays:
    pass `interventions` (ablation specs), a `tape` of recorded tool results and a new `seed`."""

    def __init__(
        self,
        models: Dict[str, ModelFn],
        tools: Optional[Dict[str, ToolFn]] = None,
        *,
        out_dir: Optional[str] = None,
        run_id: Optional[str] = None,
        task: Any = None,
        program: str = "",
        seed: int = 0,
        interventions: Sequence[str] = (),
        tape: Optional[Tape] = None,
        live_tools: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        sensitive_tools: Sequence[str] = (),
        sink: Optional[HttpSink] = None,
    ):
        self.models = dict(models)
        self.tools = dict(tools or {})
        self.run_id = run_id or time.strftime("run-%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.seed = seed
        self.interventions = list(interventions)
        self.tape = tape
        self.live_tools = live_tools
        self.events: List[Dict[str, Any]] = []
        self.blobs: Dict[str, Any] = {}
        self._prev = GENESIS
        self._agents: Dict[str, "Agent"] = {}
        self._mail: Dict[str, List[Ref]] = {}
        self._tape_seen: Dict[tuple, int] = {}
        self.path: Optional[str] = None
        self.sensitive_tools = set(sensitive_tools)
        self.sink = sink
        self._pending_blobs: Dict[str, Any] = {}
        if out_dir:
            self.path = os.path.join(out_dir, self.run_id)
            os.makedirs(os.path.join(self.path, "blobs"), exist_ok=True)
            open(os.path.join(self.path, "events.jsonl"), "w").close()
        self._emit("run", "run.start", program=program, seed=seed, interventions=self.interventions,
                   models=sorted(self.models), tools=sorted(self.tools), meta=meta or {},
                   sensitive_tools=sorted(self.sensitive_tools))
        self.task_ref: Optional[Ref] = None
        if task is not None:
            self.task_ref = self._input("run", task, source="task", trust="trusted", kind="task")

    # -- storage
    def _blob(self, value: Any) -> str:
        ref = content_hash(value)
        if ref not in self.blobs:
            self.blobs[ref] = value
            if self.sink is not None:
                self._pending_blobs[ref] = value
            if self.path:
                p = os.path.join(self.path, "blobs", ref[7:] + ".json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump({"v": value}, f, sort_keys=True, ensure_ascii=False, default=str)
        return ref

    def _emit(self, agent: str, etype: str, **body: Any) -> Dict[str, Any]:
        ev = {"schema": SCHEMA, "seq": len(self.events), "id": secrets.token_hex(16), "prev_hash": self._prev,
              "ts": _now(), "run_id": self.run_id, "agent": agent, "type": etype}
        ev.update(body)
        ev["hash"] = event_hash(ev)
        self._prev = ev["hash"]
        self.events.append(ev)
        if self.path:
            with open(os.path.join(self.path, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, sort_keys=True, ensure_ascii=False) + "\n")
        if self.sink is not None:
            self.sink.write(ev, self._pending_blobs)
            self._pending_blobs = {}
        return ev

    def _input(self, agent: str, value: Any, *, source: str, trust: str, kind: str = "input") -> Ref:
        if trust not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {TRUST_LEVELS}")
        ref = self._blob(value)
        ev = self._emit(agent, "input", ref=ref, source=source, trust=trust, kind=kind)
        return Ref(ref, value, kind, agent, source, trust, ev["id"])

    def _ablated(self, r: Ref) -> bool:
        return any(ablation_matches(s, r) for s in self.interventions)

    # -- public
    def agent(self, name: str, *, parent: Optional[str] = None, role: str = "") -> "Agent":
        if name in self._agents:
            return self._agents[name]
        self._emit(name, "agent.start", parent=parent, role=role)
        a = Agent(self, name)
        self._agents[name] = a
        return a

    def finish(self, outcome: Any = None) -> Run:
        self._emit("run", "run.end", outcome=outcome)
        if self.sink is not None:
            self.sink.flush()
        return self.run()

    def run(self) -> Run:
        return Run(self.run_id, self.events, self.blobs, self.path)


class Agent:
    def __init__(self, rt: Runtime, name: str):
        self.rt = rt
        self.name = name
        self._n_decisions = 0

    def observe(self, value: Any, *, source: str, trust: str = "untrusted") -> Ref:
        """Bring an outside value into this agent's world (file, web page, email, user message)."""
        return self.rt._input(self.name, value, source=source, trust=trust)

    def decide(self, context: Iterable[Ref], *, model: str, purpose: str = "", params: Optional[dict] = None) -> Ref:
        """One model call. `context` is exactly what the model saw; under an intervention,
        matching refs are removed before the call and listed in `ablated`."""
        rt = self.rt
        context = [c for c in context if c is not None]
        kept = [c for c in context if not rt._ablated(c)]
        ablated = [c.ref for c in context if rt._ablated(c)]
        seed = derive_seed(rt.seed, self.name, self._n_decisions)
        self._n_decisions += 1
        params = params or {}
        if model not in rt.models:
            raise KeyError(f"model {model!r} not registered")
        t0 = time.perf_counter()
        try:
            value = rt.models[model]([c.value for c in kept], purpose=purpose, seed=seed, params=params,
                                     agent=self.name)
        except Exception as e:
            self.record_decision(kept, {"error": f"{type(e).__name__}: {e}"}, model=model, purpose=purpose,
                                 params=params, seed=seed, ablated=ablated, status="error",
                                 duration_ms=(time.perf_counter() - t0) * 1000)
            raise
        usage = {}
        if isinstance(value, ModelOutput):
            value, usage = value.value, value.usage
        return self.record_decision(kept, value, model=model, purpose=purpose, params=params, seed=seed,
                                    ablated=ablated, usage=usage, duration_ms=(time.perf_counter() - t0) * 1000)

    def record_decision(self, context: Iterable[Ref], output: Any, *, model: str, purpose: str = "",
                        params: Optional[dict] = None, seed: Optional[int] = None, ablated: Sequence[str] = (),
                        usage: Optional[dict] = None, duration_ms: Optional[float] = None,
                        status: str = "ok") -> Ref:
        """Record a model call you made yourself (adapters use this). Replay of such a decision
        needs the model to be registered under the same name."""
        rt = self.rt
        kept = [c for c in context if c is not None]
        out = rt._blob(output)
        ev = rt._emit(self.name, "decision", model=model, purpose=purpose, params=params or {}, seed=seed,
                      context=[{"ref": c.ref, "kind": c.kind, "source": c.source, "trust": c.trust,
                                "from_event": c.event_id} for c in kept],
                      ablated=list(ablated), output=out, usage=usage or {}, status=status,
                      duration_ms=round(duration_ms, 3) if duration_ms is not None else None)
        trust = "untrusted" if any(c.trust == "untrusted" for c in kept) else "trusted"
        return Ref(out, output, "output", self.name, purpose, trust, ev["id"])

    def act(self, tool: str, args: Optional[dict] = None, *, decision: Optional[Ref] = None,
            sensitive: Optional[bool] = None) -> Ref:
        """Run a tool. Under replay, results come from the tape when the call matches a recorded one."""
        rt = self.rt
        args = args or {}
        args_ref = rt._blob(args)
        mode = "live"
        t0 = time.perf_counter()
        result: Any = _MISSING
        if rt.tape is not None:
            key = (self.name, tool, args_ref)
            n = rt._tape_seen.get(key, 0)
            rt._tape_seen[key] = n + 1
            result = rt.tape.get(key + (n,))
            mode = "tape"
        if result is _MISSING:
            if rt.live_tools and tool in rt.tools:
                mode = "live"
                try:
                    result = rt.tools[tool](**args)
                except Exception as e:  # recorded, not raised: the log is the point
                    result = {"error": f"{type(e).__name__}: {e}"}
            else:
                mode = "stub"
                result = {"_unrecorded": True, "tool": tool}
        status = "error" if isinstance(result, dict) and "error" in result else "ok"
        res_ref = rt._blob(result)
        ev = rt._emit(self.name, "action", tool=tool, args_ref=args_ref, result_ref=res_ref,
                      decision=decision.event_id if decision else None, status=status, mode=mode,
                      sensitive=bool(sensitive) if sensitive is not None else tool in rt.sensitive_tools,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 3))
        trust = decision.trust if decision else "trusted"
        return Ref(res_ref, result, "result", self.name, tool, trust, ev["id"])

    def record_action(self, tool: str, args: Optional[dict], result: Any, *, decision: Optional[Ref] = None,
                      sensitive: Optional[bool] = None, duration_ms: Optional[float] = None,
                      status: Optional[str] = None) -> Ref:
        """Record a tool call your own code executed (adapters use this)."""
        rt = self.rt
        args_ref, res_ref = rt._blob(args or {}), rt._blob(result)
        if status is None:
            status = "error" if isinstance(result, dict) and "error" in result else "ok"
        ev = rt._emit(self.name, "action", tool=tool, args_ref=args_ref, result_ref=res_ref,
                      decision=decision.event_id if decision else None, status=status, mode="live",
                      sensitive=bool(sensitive) if sensitive is not None else tool in rt.sensitive_tools,
                      duration_ms=round(duration_ms, 3) if duration_ms is not None else None)
        return Ref(res_ref, result, "result", self.name, tool, decision.trust if decision else "trusted", ev["id"])

    def send(self, to: str, content: Any, *, decision: Optional[Ref] = None) -> Ref:
        """Message another agent. Delivered refs carry the channel (from->to) so a channel
        can be ablated in replay (`msg:researcher->planner`)."""
        rt = self.rt
        value = content.value if isinstance(content, Ref) else content
        trust = content.trust if isinstance(content, Ref) else "trusted"
        if decision is None and isinstance(content, Ref) and content.kind == "output":
            decision = content
        ref = rt._blob(value)
        ev = rt._emit(self.name, "message", to=to, ref=ref, decision=decision.event_id if decision else None,
                      trust=trust)
        r = Ref(ref, value, "message", to, f"{self.name}->{to}", trust, ev["id"])
        rt._mail.setdefault(to, []).append(r)
        return r

    def inbox(self) -> List[Ref]:
        return list(self.rt._mail.get(self.name, []))
