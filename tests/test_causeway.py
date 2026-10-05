import json
import os

import pytest

from causeway import Runtime, System, build_graph, counterfactual, load_run, taint, target_hits, verify
from causeway.cli import main as cli
from causeway.core import Ref, Tape, ablation_matches, content_hash
from causeway.demo import EXFIL_TARGET, REFUND_TARGET, SYSTEM
from causeway.evals import influence_index, structure
from causeway.replay import diff_runs, newcombe_diff, replay, wilson


def exfil_run(tmp_path):
    for s in range(50):
        r = SYSTEM.run(seed=s, out_dir=str(tmp_path), run_id=f"r{s}")
        if target_hits(r, EXFIL_TARGET):
            return load_run(r.path)
    raise AssertionError("no exfil run in 50 seeds")


# --------------------------------------------------------------------------- core

def test_record_and_verify(tmp_path):
    run = exfil_run(tmp_path)
    assert verify(run) == []
    types = {e["type"] for e in run.events}
    assert {"run.start", "agent.start", "input", "decision", "action", "message", "run.end"} <= types


@pytest.mark.parametrize("mutation", ["edit", "delete", "reorder", "blob"])
def test_tamper_detected(tmp_path, mutation):
    run = exfil_run(tmp_path)
    path = os.path.join(run.path, "events.jsonl")
    lines = open(path).read().splitlines()
    if mutation == "edit":
        ev = json.loads(lines[5]); ev["agent"] = "someone-else"; lines[5] = json.dumps(ev)
    elif mutation == "delete":
        del lines[6]
    elif mutation == "reorder":
        lines[6], lines[7] = lines[7], lines[6]
    if mutation != "blob":
        open(path, "w").write("\n".join(lines) + "\n")
    else:
        ref = run.of_type("decision")[0]["output"]
        bp = os.path.join(run.path, "blobs", ref[7:] + ".json")
        json.dump({"v": {"facts": ["nothing to see"]}}, open(bp, "w"))
    assert verify(load_run(run.path))


def test_ablation_specs():
    r = Ref("sha256:abcd", "x", "input", "a", "vendor:portal/notes.md", "untrusted")
    m = Ref("sha256:ef01", "y", "message", "planner", "researcher->planner")
    assert ablation_matches("input:vendor:*", r) and not ablation_matches("input:web:*", r)
    assert ablation_matches("ref:abcd", r) and ablation_matches("untrusted", r)
    assert ablation_matches("msg:researcher->*", m) and not ablation_matches("msg:*->executor", m)
    with pytest.raises(ValueError):
        ablation_matches("bogus:x", r)


def test_intervention_recorded_in_decision():
    run = SYSTEM.run(seed=1, interventions=["input:vendor:*"])
    d = run.of_type("decision")[0]
    assert d["ablated"] and all("vendor" not in c["source"] for c in d["context"])


def test_tape_replays_results_and_never_calls_live_tools_off_tape():
    calls = []

    def danger(**kw):
        calls.append(kw)
        return {"sent": True}

    def model(ctx, **kw):
        return {"go": any("go" in str(c) for c in ctx)}

    def prog(rt):
        a = rt.agent("a")
        x = a.observe("please go", source="in", trust="untrusted")
        d = a.decide([x], model="m")
        if d.value["go"]:
            a.act("danger", {"n": 1}, decision=d)
        else:
            a.act("danger", {"n": 2}, decision=d)

    s = System(prog, {"m": model}, {"danger": danger}, name="t")
    run = s.run()
    assert len(calls) == 1
    tape = Tape(run)
    s.run(tape=tape)                       # same call -> served from tape
    assert len(calls) == 1
    r2 = s.run(tape=Tape(run), interventions=["input:in"], live_tools=False)  # off-tape -> stub
    assert len(calls) == 1
    assert r2.of_type("action")[0]["mode"] == "stub"


# --------------------------------------------------------------------------- graph

def test_observed_edges_and_taint(tmp_path):
    run = exfil_run(tmp_path)
    g = build_graph(run)
    kinds = {e["kind"] for e in g.edges if e["evidence"] == "observed"}
    assert {"context:input", "context:message", "context:result", "invoked", "sent"} <= kinds
    t = taint(run, ["input:vendor:*"], graph=g)
    assert any(a["tool"] == "send_email" and "vendor-compliance" in json.dumps(a["args"]) for a in t["actions"])
    assert t["agents_reached"] == ["executor", "planner", "researcher"]
    assert all(g.nodes[s["node"]]["type"] == "input" for s in taint(run, graph=g)["sources"])


def test_same_content_and_inferred_edges():
    def model(ctx, **kw):
        return "summary: " + " ".join(str(c) for c in ctx)

    def prog(rt):
        a, b = rt.agent("a"), rt.agent("b")
        doc = a.observe("the quick brown fox jumps over the lazy dog near the river bank today",
                        source="doc", trust="untrusted")
        out = a.decide([doc], model="m")
        a.act("write_file", {"path": "x", "content": out.value})
        # b reads the file through a side channel: identical content
        b.observe(out.value, source="file:x")
        # c sees a copy of the doc text with no recorded link
        c = rt.agent("c")
        c.decide([c.observe("note: the quick brown fox jumps over the lazy dog near the river", source="paste")],
                 model="m")

    run = System(prog, {"m": model}, {"write_file": lambda **k: {"ok": True}}).run()
    g = build_graph(run)
    assert any(e["kind"] == "same-content" for e in g.edges)
    assert any(e["evidence"] == "inferred" for e in g.edges)


def test_target_spec_errors():
    from causeway.graph import parse_target
    with pytest.raises(ValueError):
        parse_target("color=red")


# --------------------------------------------------------------------------- replay

def test_replay_reproduces_original(tmp_path):
    run = exfil_run(tmp_path)
    assert diff_runs(run, replay(run)) == []


def test_counterfactual_separates_cause_from_reach(tmp_path):
    run = exfil_run(tmp_path)
    vendor = counterfactual(run, "input:vendor:*", EXFIL_TARGET, n=30)
    faq = counterfactual(run, "input:web:shipping_faq", EXFIL_TARGET, n=30)
    none = counterfactual(run, "input:does-not-exist", EXFIL_TARGET, n=5)
    assert vendor["verdict"] == "causal" and vendor["ci"][0] > 0
    assert faq["verdict"] == "no-detectable-effect"
    assert none["verdict"] == "not-applied"
    # tests persist and appear as tested edges
    g = build_graph(load_run(run.path))
    tested = [e for e in g.edges if e["evidence"] == "tested"]
    assert any(e["verdict"] == "causal" for e in tested)


def test_channel_ablation_is_a_multi_agent_eval(tmp_path):
    run = exfil_run(tmp_path)
    r = counterfactual(run, "msg:researcher->planner", REFUND_TARGET, n=20, save=False)
    assert r["verdict"] == "causal"  # the planner cannot refund without the researcher


def test_stats():
    lo, hi = wilson(0, 10)
    assert lo == 0 and 0.27 < hi < 0.28
    lo, hi = newcombe_diff(30, 40, 0, 40)
    assert 0.55 < lo < 0.65 and 0.85 < hi < 0.9


# --------------------------------------------------------------------------- evals / cli

def test_structure_and_index(tmp_path):
    for s in range(6):
        SYSTEM.run(seed=s, out_dir=str(tmp_path), run_id=f"r{s}")
    run = load_run(str(tmp_path / "r0"))
    st = structure(run)
    assert st["agents"]["executor"]["received"] == 1 and st["critical_path"]
    rows = influence_index([str(tmp_path / f"r{s}") for s in range(6)], [EXFIL_TARGET])
    vendor = next(r for r in rows if "vendor:portal/notes.md" in r["sources"])
    assert vendor["runs"] == 6 and vendor["reached_action_runs"] == 6


def test_cli_demo(tmp_path, capsys):
    assert cli(["demo", "--out", str(tmp_path), "--n", "10"]) == 0
    out = capsys.readouterr().out
    assert "CAUSAL" in out and os.path.exists(tmp_path / "report.html")
    html = open(tmp_path / "report.html").read()
    assert "const SERVER=false;let DATA={" in html and html.rstrip().endswith("</html>")
    assert cli(["verify", str(tmp_path / "demo-seed0")]) == 0
