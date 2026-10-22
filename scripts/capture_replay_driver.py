"""Driver for scripts/capture-replay.sh; argv: <hooks dir> <transcript> <project> <scratch dir>."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

hooks, transcript, project, scratch = sys.argv[1], Path(sys.argv[2]).resolve(), sys.argv[3], Path(sys.argv[4])
sys.path.insert(0, hooks)

import obsidian_utils  # noqa: E402

live = obsidian_utils.load_config()
vault = scratch / "vault"
if not vault.exists():
    shutil.copytree(live["vault_path"], vault, symlinks=True, ignore=shutil.ignore_patterns(".obsidian", ".git"))
secure = scratch / "secure"
secure.mkdir(exist_ok=True, mode=0o700)
os.environ["OBSIDIAN_BRAIN_DB"] = str(scratch / "index.db")
obsidian_utils._SECURE_DIR = str(secure)
config = {**live, "vault_path": str(vault)}
obsidian_utils.load_config = lambda fresh=False: config

import capture_state  # noqa: E402
import capture_worker  # noqa: E402

log = scratch / "replay.log"
capture_state.append_capture_log = lambda p, s, outcome, detail="": log.open("a").write(f"{outcome} {detail}\n")

# Model cost, from each worker's --output-format json envelope.
costs = []
_run = capture_worker.subprocess.run


def _metered(*args, **kwargs):
    result = _run(*args, **kwargs)
    try:
        costs.append(float(json.loads(result.stdout).get("total_cost_usd") or 0))
    except (ValueError, TypeError, AttributeError):
        costs.append(0.0)
    return result


capture_worker.subprocess.run = _metered
# CAPTURE_REPLAY_NO_EARLIER=1 replays without the session's earlier titles in the prompt (the pre-#427 baseline).
if os.environ.get("CAPTURE_REPLAY_NO_EARLIER") == "1":
    capture_worker.earlier_titles = lambda sid: []

sid = transcript.stem
size = transcript.stat().st_size
print(f"scratch={scratch} transcript={transcript.name} bytes={size}", flush=True)
for batch in range(1, 50):
    before = capture_state.get_state(sid)["offset"]
    if before >= size:
        break
    t0 = time.time()
    outcome = capture_worker.run_batch(sid, transcript, project, config)
    after = capture_state.get_state(sid)["offset"]
    print(f"batch {batch}: {outcome} offset {before}->{after} in {time.time() - t0:.0f}s", flush=True)
    if after == before:
        print("no progress; stopping", file=sys.stderr)
        break

rows = capture_state.ledger_list(sid, 1000)
print(json.dumps({"captures": len(rows),
                  "new": sum(r.get("kind") == "new" for r in rows),
                  "updated": sum(r.get("kind") == "updated" for r in rows),
                  "worker_calls": len(costs), "cost_usd": round(sum(costs), 4)}))
for r in rows:
    print(f"  {r.get('kind'):7} {r.get('type'):9} {r.get('title')}  ->  {r.get('note_path')}")
