from bench.run import attribute, build_system, score
from bench import models
from bench.scenarios import SCENARIOS
from causeway import load_run
from causeway.graph import target_hits
from causeway.replay import benjamini_hochberg, mcnemar_exact


def test_exact_tests():
    assert mcnemar_exact(5, 0) == 0.0625          # 5 one-sided flips can never reach 0.05
    assert mcnemar_exact(6, 0) < 0.05
    assert mcnemar_exact(0, 0) == 1.0
    assert benjamini_hochberg([0.001, 0.04, 0.5, 0.03]) == [True, False, False, False]


def test_bench_attributes_refund_override(tmp_path):
    sc = SCENARIOS["refund-override"]
    system = build_system(sc, models.sim_model(sc, models.Counter()))
    for seed in range(20):
        r = system.run(seed=seed, out_dir=str(tmp_path), run_id=f"s{seed}")
        if target_hits(r, sc.harmful):
            break
    run = load_run(r.path)
    att = attribute(run, sc, system, n=20)
    assert att["causeway"] == sc.truth
    assert att["provenance"] == set()            # nothing for string matching to find here
    assert len(att["reach"]) == 3
    assert score(att["causeway"], sc.truth)["exact"] == 1


def test_control_has_no_harmful_action(tmp_path):
    sc = SCENARIOS["control-clean"]
    system = build_system(sc, models.sim_model(sc, models.Counter()))
    for seed in range(10):
        r = system.run(seed=seed)
        assert not target_hits(r, SCENARIOS["exfil-vendor-note"].harmful)
