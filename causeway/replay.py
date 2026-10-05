"""Replay and counterfactual tests.

A recorded run names the program that produced it. Replay re-executes that program with
tool results served from the run's tape (so nothing real is re-sent) and model calls made
again with fresh seeds. A counterfactual test runs N paired trials with and without an
intervention (e.g. remove one document) and reports how often a target action happens in each.
"""
from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .core import ModelOutput, Ref, Run, Runtime, Tape, ablation_matches, canonical, derive_seed
from .graph import parse_target

Z95 = 1.959963984540054


@dataclass
class System:
    """A replayable multi-agent program: program(rt) plus the models and tools it uses."""

    program: Callable[[Runtime], Any]
    models: Dict[str, Callable[..., Any]]
    tools: Dict[str, Callable[..., Any]] = field(default_factory=dict)
    task: Any = None
    name: str = ""
    sensitive_tools: Sequence[str] = ()

    def run(self, *, seed: int = 0, interventions: Sequence[str] = (), tape: Optional[Tape] = None,
            out_dir: Optional[str] = None, live_tools: bool = True, run_id: Optional[str] = None,
            task: Any = None, sink: Any = None) -> Run:
        rt = Runtime(self.models, self.tools, out_dir=out_dir, run_id=run_id,
                     task=self.task if task is None else task, program=self.name, seed=seed,
                     interventions=interventions, tape=tape, live_tools=live_tools,
                     sensitive_tools=self.sensitive_tools, sink=sink)
        outcome = self.program(rt)
        return rt.finish(outcome)


def load_system(spec: str) -> System:
    mod, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(mod), attr or "SYSTEM")
    if not isinstance(obj, System):
        raise TypeError(f"{spec} is not a causeway System")
    if not obj.name:
        obj.name = spec
    return obj


def system_for(run: Run) -> System:
    prog = run.start.get("program")
    if not prog:
        raise ValueError("run has no program recorded; pass a System explicitly")
    return load_system(prog)


def recorded_task(run: Run) -> Any:
    for e in run.of_type("input"):
        if e.get("kind") == "task":
            return run.value(e["ref"])
    return None


# --------------------------------------------------------------------------- stats

def wilson(k: int, n: int, z: float = Z95):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on discordant pairs: b pairs where the target happened only with
    the item, c pairs where it happened only without it. Returns the p-value."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def benjamini_hochberg(pvalues: List[float], q: float = 0.05) -> List[bool]:
    """Which hypotheses to reject while controlling the false discovery rate at q."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    cutoff = 0
    for rank, i in enumerate(order, 1):
        if pvalues[i] <= q * rank / m:
            cutoff = rank
    keep = set(order[:cutoff])
    return [i in keep for i in range(m)]


def newcombe_diff(k0: int, n0: int, k1: int, n1: int, z: float = Z95):
    """95% CI for p0 - p1 (Newcombe hybrid score method)."""
    p0, p1 = k0 / n0, k1 / n1
    l0, u0 = wilson(k0, n0, z)
    l1, u1 = wilson(k1, n1, z)
    d = p0 - p1
    lo = d - math.sqrt((p0 - l0) ** 2 + (u1 - p1) ** 2)
    hi = d + math.sqrt((u0 - p0) ** 2 + (p1 - l1) ** 2)
    return (max(-1.0, lo), min(1.0, hi))


# --------------------------------------------------------------------------- counterfactual

def counterfactual(run: Run, intervention: str, target: str, *, n: int = 30, system: Optional[System] = None,
                   use_tape: bool = True, save: bool = True, seed_base: str = "cf",
                   live_tools: bool = False) -> Dict[str, Any]:
    """Does removing `intervention` change how often `target` happens?

    Paired design: trial i uses the same seed with and without the intervention, so the only
    difference between a pair is the intervention (common random numbers).

    Tool calls that match the recorded tape replay its results. Calls the original run never made
    are stubbed unless live_tools=True, so a replay cannot send a real email or move real money."""
    system = system or system_for(run)
    pred = parse_target(target)
    task = recorded_task(run)

    def hit(r: Run) -> bool:
        return any(pred(e, r) for e in r.events)

    k0 = k1 = 0
    matched = 0
    off_tape = 0
    flips = {"removed": 0, "added": 0}
    for i in range(n):
        s = derive_seed(seed_base, run.run_id, i)
        base = system.run(seed=s, tape=Tape(run) if use_tape else None, task=task, run_id=f"{run.run_id}-b{i}",
                          live_tools=live_tools)
        treat = system.run(seed=s, interventions=[intervention], tape=Tape(run) if use_tape else None, task=task,
                           run_id=f"{run.run_id}-t{i}", live_tools=live_tools)
        h0, h1 = hit(base), hit(treat)
        k0 += h0
        k1 += h1
        if h0 and not h1:
            flips["removed"] += 1
        if h1 and not h0:
            flips["added"] += 1
        if any(e.get("ablated") for e in treat.of_type("decision")):
            matched += 1
        off_tape += sum(1 for e in treat.of_type("action") if e.get("mode") != "tape")
    p0, p1 = k0 / n, k1 / n
    lo, hi = newcombe_diff(k0, n, k1, n)
    if matched == 0:
        verdict = "not-applied"
    elif lo > 0:
        verdict = "causal"
    elif hi < 0:
        verdict = "suppressive"
    else:
        verdict = "no-detectable-effect"
    result = {
        "intervention": intervention, "target": target, "n": n,
        "p_with": round(p0, 4), "p_without": round(p1, 4),
        "effect": round(p0 - p1, 4), "ci": [round(lo, 4), round(hi, 4)],
        "verdict": verdict, "pair_flips": flips,
        "p_value": round(mcnemar_exact(flips["removed"], flips["added"]), 6),
        "intervention_applied_in": matched, "off_tape_actions": off_tape,
        "method": "paired re-execution, tool results from tape, Newcombe 95% CI", "scope": "run",
    }
    if save:
        run.add_test(result)
    return result


def decision_test(run: Run, decision: Any, intervention: str, *, n: int = 30,
                  models: Optional[Dict[str, Callable[..., Any]]] = None, system: Optional[System] = None,
                  contains: Optional[str] = None, save: bool = True) -> Dict[str, Any]:
    """Direct effect on ONE recorded model call, using only the log: re-call the model on the recorded
    context with and without the matching items. No need to re-run the program, so this works for
    systems that cannot be replayed end to end. `decision` is a seq number or event id.

    With `contains`, measures P(output contains text). Without it, measures how often removing the
    item changes the output relative to the same-seed call with the full context."""
    ev = next((e for e in run.of_type("decision") if e["seq"] == decision or e["id"] == decision), None)
    if ev is None:
        raise ValueError(f"no decision {decision!r} in run")
    if models is None:
        models = (system or system_for(run)).models
    fn = models.get(ev["model"])
    if fn is None:
        raise KeyError(f"model {ev['model']!r} is not available for replay")
    ctx = [(Ref(c["ref"], run.value(c["ref"]), c.get("kind", "input"), ev["agent"], c.get("source", ""),
                c.get("trust", "trusted")), run.value(c["ref"])) for c in ev["context"]]
    full = [v for _, v in ctx]
    kept = [v for r, v in ctx if not ablation_matches(intervention, r)]
    applied = len(kept) < len(full)

    def call(values, seed):
        out = fn(values, purpose=ev.get("purpose", ""), seed=seed, params=ev.get("params") or {}, agent=ev["agent"])
        return out.value if isinstance(out, ModelOutput) else out

    k0 = k1 = 0
    for i in range(n):
        s = derive_seed("dt", run.run_id, ev["seq"], i)
        a, b = call(full, s), call(kept, s)
        if contains is not None:
            k0 += contains in canonical(a).decode()
            k1 += contains in canonical(b).decode()
        else:
            k1 += canonical(a) != canonical(b)
    if contains is not None:
        lo, hi = newcombe_diff(k0, n, k1, n)
        p0, p1, eff = k0 / n, k1 / n, (k0 - k1) / n
        target = f"decision#{ev['seq']} output~{contains}"
    else:
        lo, hi = wilson(k1, n)
        p0, p1, eff = None, None, k1 / n
        target = f"decision#{ev['seq']} output changed"
    verdict = "not-applied" if not applied else ("causal" if lo > 0 else "suppressive" if hi < 0 else
                                                  "no-detectable-effect")
    result = {"intervention": intervention, "target": target, "n": n, "p_with": p0, "p_without": p1,
              "effect": round(eff, 4), "ci": [round(lo, 4), round(hi, 4)], "verdict": verdict,
              "scope": "decision", "decision": ev["id"], "decision_seq": ev["seq"],
              "method": "re-call one recorded model call with and without the item, paired seeds"}
    if save:
        run.add_test(result)
    return result


def replay(run: Run, *, system: Optional[System] = None, seed: Optional[int] = None,
           interventions: Sequence[str] = ()) -> Run:
    """Re-run once. With the original seed and no intervention, a deterministic program reproduces
    the original decisions; `diff_runs` shows where it did not."""
    system = system or system_for(run)
    task = recorded_task(run)
    return system.run(seed=run.start.get("seed", 0) if seed is None else seed, interventions=interventions,
                      tape=Tape(run), task=task, run_id=run.run_id + "-replay", live_tools=False)


def diff_runs(a: Run, b: Run) -> List[Dict[str, Any]]:
    """Align decisions and actions by (agent, type, ordinal) and report the first divergences."""
    def key_events(r: Run):
        counts: Dict[tuple, int] = {}
        out = {}
        for e in r.events:
            if e["type"] not in ("decision", "action", "message"):
                continue
            k = (e["agent"], e["type"])
            i = counts.get(k, 0)
            counts[k] = i + 1
            sig = e.get("output") or (e.get("tool"), e.get("args_ref")) or e.get("ref")
            if e["type"] == "message":
                sig = (e["to"], e["ref"])
            out[k + (i,)] = (e, sig)
        return out

    ka, kb = key_events(a), key_events(b)
    diffs = []
    for k in sorted(set(ka) | set(kb), key=lambda k: (ka.get(k, kb.get(k))[0]["seq"])):
        ea, eb = ka.get(k), kb.get(k)
        if ea is None or eb is None:
            diffs.append({"at": list(k), "change": "only-in-" + ("replay" if ea is None else "original")})
        elif ea[1] != eb[1]:
            diffs.append({"at": list(k), "change": "different"})
    return diffs
