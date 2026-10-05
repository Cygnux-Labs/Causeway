"""causeway command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from .core import load_run, verify
from .evals import influence_index, influence_matrix, list_runs, structure
from .graph import build_graph, taint, target_hits
from .replay import counterfactual, decision_test, diff_runs, load_system, replay
from .view import render_html, render_workspace


def _p(*a):
    print(*a, flush=True)


def _fmt_test(r):
    if r.get("p_with") is None:
        return (f"  remove {r['intervention']:<28} target {r['target']:<40} "
                f"changed in {r['effect']:.0%} of calls [{r['ci'][0]:.2f}, {r['ci'][1]:.2f}] n={r['n']}  "
                f"{r['verdict'].upper()}")
    return (f"  remove {r['intervention']:<28} target {r['target']:<40} "
            f"P(with)={r['p_with']:.2f} P(without)={r['p_without']:.2f} "
            f"effect={r['effect']:+.2f} [{r['ci'][0]:+.2f}, {r['ci'][1]:+.2f}] n={r['n']}  {r['verdict'].upper()}")


def cmd_demo(a):
    from .demo import EXFIL_TARGET, REFUND_TARGET, SYSTEM
    out = a.out or tempfile.mkdtemp(prefix="causeway-")
    _p(f"== record: 8 runs of a 3-agent support system (seeded mock models, fake tools) -> {out}")
    exfil = []
    for s in range(8):
        r = SYSTEM.run(seed=s, out_dir=out, run_id=f"demo-seed{s}")
        hit = bool(target_hits(r, EXFIL_TARGET))
        if hit:
            exfil.append(load_run(r.path))
        _p(f"  demo-seed{s}: {', '.join(a['tool'] for a in r.of_type('action'))}{'   <- exfiltration' if hit else ''}")
    assert exfil, "no exfiltration in 8 seeds"
    run = exfil[0]
    _p(f"\n== investigating {run.run_id}")
    for e in run.of_type("action"):
        _p(f"  {e['agent']:<10} {e['tool']:<16} {json.dumps(run.value(e['args_ref']))}")
    problems = verify(run)
    _p(f"\n== verify: {'hash chain intact, ' + str(len(run.events)) + ' events' if not problems else problems}")

    _p("\n== blast radius (observed edges): what did each untrusted input reach?")
    g = build_graph(run)
    for n in g.nodes.values():
        if n["type"] == "input" and n["trust"] == "untrusted":
            t = taint(run, [f"input:{n['label']}"], graph=g)
            _p(f"  {n['label']:<24} reached {len(t['actions'])} actions: {', '.join(x['tool'] for x in t['actions'])}")
    _p("  Every untrusted input reached the exfiltration email. Reach alone cannot say which one caused it.")

    _p(f"\n== counterfactual tests (n={a.n} paired replays each; tools served from tape)")
    tests = [("input:vendor:portal/notes.md", EXFIL_TARGET), ("input:web:shipping_faq", EXFIL_TARGET),
             ("input:inbox:ticket-881", EXFIL_TARGET), ("msg:researcher->planner", EXFIL_TARGET),
             ("input:kb:refund_policy.md", REFUND_TARGET)]
    for spec, target in tests:
        _p(_fmt_test(counterfactual(run, spec, target, n=a.n)))
    if len(exfil) > 1:  # a second run, so the cross-run influence view has more than one data point
        for spec in ("input:vendor:portal/notes.md", "input:web:shipping_faq"):
            counterfactual(exfil[1], spec, EXFIL_TARGET, n=a.n)
    dec = run.of_type("decision")[0]
    _p("\n== decision-level test (re-call only the researcher's model call; no program re-run needed)")
    _p(_fmt_test(decision_test(run, dec["seq"], "input:vendor:*", n=a.n, contains="vendor-compliance")))

    st = structure(run)
    _p("\n== structure")
    _p("  critical path: " + " -> ".join(st["critical_path"]))
    html = os.path.join(out, "report.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write(render_workspace(out))
    _p(f"\nruns:   {out}\nreport: {html}   (static; open in a browser)")
    _p(f"live:   causeway serve {out} --allow-program causeway.demo:SYSTEM")


def cmd_verify(a):
    run = load_run(a.run)
    problems = verify(run)
    if problems:
        for p in problems:
            _p("[FAIL] " + p)
        return 1
    _p(f"[PASS] {len(run.events)} events, chain intact, {len(run.blobs)} blobs match their hashes")
    _p("       (unsigned; use Tracekit to sign and witness the log)")
    return 0


def cmd_graph(a):
    g = build_graph(load_run(a.run), infer=not a.no_infer)
    if a.json:
        d = g.to_dict()
        for n in d["nodes"]:
            n.pop("event", None)
        _p(json.dumps(d, indent=2, default=str))
        return
    for e in g.edges:
        s, t = g.nodes[e["src"]], g.nodes[e["dst"]]
        extra = "".join(f" {k}={e[k]}" for k in ("reuse", "score", "effect") if k in e)
        _p(f"{s['label']}[{s['agent']}] -> {t['label']}[{t['agent']}]  {e['kind']} ({e['evidence']}){extra}")


def cmd_taint(a):
    r = taint(load_run(a.run), a.source or ["untrusted"], include_inferred=a.inferred)
    if a.json:
        _p(json.dumps(r, indent=2, default=str))
        return
    _p("sources: " + ", ".join(s["label"] for s in r["sources"]))
    _p("agents reached: " + ", ".join(r["agents_reached"]))
    for x in r["actions"]:
        _p(f"  #{x['seq']} {x['agent']}.{x['tool']} {json.dumps(x['args'])}\n      via " + " > ".join(x["path"]))


def cmd_test(a):
    run = load_run(a.run)
    system = load_system(a.system) if a.system else None
    if a.decision is not None:
        r = decision_test(run, a.decision, a.remove, n=a.n, system=system, contains=a.contains)
    else:
        if not a.target:
            raise SystemExit("--target is required unless --decision is given")
        r = counterfactual(run, a.remove, a.target, n=a.n, system=system, live_tools=a.live_tools)
    _p(json.dumps(r, indent=2) if a.json else _fmt_test(r))


def cmd_matrix(a):
    run = load_run(a.run)
    rows = influence_matrix(run, a.target, n=a.n, system=load_system(a.system) if a.system else None)
    for r in rows:
        _p(_fmt_test(r))


def cmd_replay(a):
    run = load_run(a.run)
    rep = replay(run, system=load_system(a.system) if a.system else None, interventions=a.remove or [])
    d = diff_runs(run, rep)
    _p("replay matches the original" if not d else json.dumps(d, indent=2))


def cmd_record(a):
    sysm = load_system(a.system)
    r = sysm.run(seed=a.seed, out_dir=a.out)
    _p(r.path)


def cmd_view(a):
    run = load_run(a.run)
    out = a.output or os.path.join(a.run, "view.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(run))
    _p(out)


def cmd_report(a):
    out = a.output or os.path.join(a.root, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_workspace(a.root))
    _p(out)


def cmd_serve(a):
    from .server import serve
    token = a.token or os.environ.get("CAUSEWAY_TOKEN")
    httpd, _ = serve(a.root, a.host, a.port, token, a.allow_program or [])
    _p(f"Causeway on http://{a.host}:{a.port}  (runs: {a.root})")
    _p(f"  ingest: {'POST /v1/ingest with Bearer token' if token else 'disabled (set --token or CAUSEWAY_TOKEN)'}")
    _p(f"  replay from the UI: {', '.join(a.allow_program) if a.allow_program else 'disabled (--allow-program module:SYSTEM)'}")
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        _p("  WARNING: the app and read API have no auth; keep them behind a trusted network or proxy.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_index(a):
    rows = influence_index(list_runs(a.root), a.target or [])
    if a.json:
        _p(json.dumps(rows, indent=2, default=str))
        return
    for r in rows[: a.top]:
        best = max(r["tested"], key=lambda t: t["effect"], default=None)
        tested = f"max effect {best['effect']:+.2f} on {best['target']}" if best else "untested"
        _p(f"{r['ref'][:19]} {','.join(r['sources']):<28} {r['trust']:<9} in {r['runs']} runs, "
           f"reached actions in {r['reached_action_runs']}, {tested}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="causeway", description="Causal logs, replay and investigation for multi-agent systems")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("demo", help="record the 3-agent demo, test causes, write a view")
    p.add_argument("--out"); p.add_argument("--n", type=int, default=40); p.set_defaults(f=cmd_demo)

    p = sp.add_parser("record", help="run a System (module:ATTR) and record it")
    p.add_argument("system"); p.add_argument("--out", default="runs"); p.add_argument("--seed", type=int, default=0)
    p.set_defaults(f=cmd_record)

    p = sp.add_parser("verify", help="check the hash chain and blobs"); p.add_argument("run"); p.set_defaults(f=cmd_verify)

    p = sp.add_parser("graph", help="print edges with evidence grades")
    p.add_argument("run"); p.add_argument("--json", action="store_true"); p.add_argument("--no-infer", action="store_true")
    p.set_defaults(f=cmd_graph)

    p = sp.add_parser("taint", help="blast radius of sources (default: everything untrusted)")
    p.add_argument("run"); p.add_argument("--source", action="append"); p.add_argument("--inferred", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_taint)

    p = sp.add_parser("test", help="counterfactual: does removing X change whether target happens?")
    p.add_argument("run"); p.add_argument("--remove", required=True); p.add_argument("--target")
    p.add_argument("--decision", type=int, help="test one recorded model call (seq) instead of re-running the program")
    p.add_argument("--contains", help="with --decision: measure P(output contains this text)")
    p.add_argument("--n", type=int, default=30); p.add_argument("--system"); p.add_argument("--live-tools", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_test)

    p = sp.add_parser("matrix", help="test every channel and input against targets")
    p.add_argument("run"); p.add_argument("--target", action="append", required=True); p.add_argument("--n", type=int, default=20)
    p.add_argument("--system"); p.set_defaults(f=cmd_matrix)

    p = sp.add_parser("replay", help="re-run from tape and diff against the original")
    p.add_argument("run"); p.add_argument("--remove", action="append"); p.add_argument("--system"); p.set_defaults(f=cmd_replay)

    p = sp.add_parser("view", help="write the single-file investigation view")
    p.add_argument("run"); p.add_argument("-o", "--output"); p.set_defaults(f=cmd_view)

    p = sp.add_parser("report", help="static investigation app for every run under a folder")
    p.add_argument("root"); p.add_argument("-o", "--output"); p.set_defaults(f=cmd_report)

    p = sp.add_parser("serve", help="investigation app + API + ingestion endpoint")
    p.add_argument("root"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=7788)
    p.add_argument("--token", help="bearer token for /v1/ingest (or CAUSEWAY_TOKEN)")
    p.add_argument("--allow-program", action="append", help="System spec the UI may replay (repeatable)")
    p.set_defaults(f=cmd_serve)

    p = sp.add_parser("index", help="influence index across runs")
    p.add_argument("root"); p.add_argument("--target", action="append"); p.add_argument("--top", type=int, default=20)
    p.add_argument("--json", action="store_true"); p.set_defaults(f=cmd_index)

    a = ap.parse_args(argv)
    return a.f(a) or 0


if __name__ == "__main__":
    sys.exit(main())
