#!/usr/bin/env bash
# capture-replay.sh -- replay auto-capture over one transcript against a COPY of the vault (#425).
#
# A dev tool for tuning hooks/capture_worker_prompt.md. It copies the live
# vault to a scratch dir, points the capture state, ledger, index DB and hook
# log at that dir, and runs capture batches in the foreground until the whole
# transcript is read. The live vault, ledger and index are never written.
# It runs real `claude -p` workers, so each batch costs a model call.
#
# Usage: scripts/capture-replay.sh <transcript.jsonl> <project> [scratch-dir]
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <transcript.jsonl> <project> [scratch-dir]" >&2
  exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
scratch="${3:-$(mktemp -d "${TMPDIR:-/tmp}/capture-replay.XXXXXX")}"
mkdir -p "$scratch"
chmod 700 "$scratch"
exec python3 - "$here/../hooks" "$1" "$2" "$scratch" < "$here/capture_replay_driver.py"
