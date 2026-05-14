# DEV-TEST: Issue #87 — Smarter /check-items merge gate

Manual dev-test harness for the Phase H (Tasks 27–28) completion checkpoint.

Run `python3 scripts/dev-test/test-issue-87-manual.py` from the repo root.
Expected output: `5 assertions, 0 failures`, exit 0.

The harness exercises the five merge-gate conditions with real vault data and
real signatures (adapted from prototype-check-items-87.py): cold-cache
`collect_open_items` + `find_duplicates` grouping; sub-2s `partition()` from
`check_items_cache`; prompt-constant integrity for `CLASSIFIER_PROMPT` and
`SEMANTIC_MERGE_PROMPT`; dashboard directory write access; and `/recall`
SKILL.md cleanliness (no `AskUserQuestion` or `batch_cascade_checkoff`).

Config is read from `~/.claude/obsidian-brain-config.json` — no mocking.

Refs #87.
