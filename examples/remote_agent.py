"""Ship events from an agent process to a Causeway collector.

    causeway serve runs-server --token dev-token --allow-program causeway.demo:SYSTEM   # terminal 1
    python examples/remote_agent.py                                                     # terminal 2
    open http://127.0.0.1:7788

The collector checks the token, every event hash, the link to the previously stored event and
every blob hash before it writes anything.
"""
from causeway.core import HttpSink
from causeway.demo import SYSTEM

for seed in range(3):
    run = SYSTEM.run(seed=seed, run_id=f"remote-{seed}", sink=HttpSink("http://127.0.0.1:7788", "dev-token"))
    print("sent", run.run_id, len(run.events), "events")
