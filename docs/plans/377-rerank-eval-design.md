# Rerank eval: design

- **Issue:** abhattacherjee/obsidian-brain#377
- **Date:** 2026-09-28
- **Status:** draft, awaiting review
- **Related:** #376 (missing note types in `_TYPE_SCORES_BY_CONTEXT`), #54 (retrieval refinements)

## Goal

Improve **recall quality**: get the notes that answer a question into context more often.

Before changing any ranking, measure whether a decision model does better than today's ranking. The candidates are Jev (TypeSafe's cloud System One model) and laya (an open, local model with the same interface).

**Phase 1 (this spec) changes no ranking.** It adds a citation log, a seed eval set and an eval harness. Phase 2, which reranks vault-ask candidates, has its own spec and only happens if a backend passes the go gate below.

## Why measure first

- cc-token-router#143 phase A tested Jev on tier routing and it failed: it sent 67% of L3 work to L1. Jev's accuracy is not a given.
- laya's README reports **0.362** accuracy zero-shot and **0.766** after fine-tuning on the domain (2,000 typed decisions). Zero-shot laya may be too weak, and fine-tuning needs labelled data we don't have yet.
- Jev sends note text to a third party. That cost is only worth paying if Jev clearly wins.

## Scope

In scope, both about retrieval:

1. **Candidate relevance in `/vault-ask`.** Today Step 5 of `skills/vault-ask/SKILL.md` ranks candidates with an additive rule table: +2 per term, +3 for insight/decision/error-fix types, +1 for session, +1 within 30 days, +2 for a tag match. It keeps the top 10.
2. **Question type → note-type weighting.** `detect_task_context` (`hooks/vault_index.py:1464`) picks a context from the git branch and the `caller` string, never from the question text. vault-ask passes no `caller`, so `_TYPE_SCORES_BY_CONTEXT` (`vault_index.py:1482`) goes unused.

Out of scope:

- `memory_inject.py`. It lives in the global `~/.claude` setup, not this repo.
- check-items. Swapping its classifier for Jev is a cost goal, not a recall goal.
- Grounding checks on answers. That is answer quality, not recall.
- Any change to `search_vault`, `/recall` or `/compress` ranking.

## Components

### 1. Citation log: `hooks/ask_log.py`

vault-ask gets a new last step that calls `ask_log.py record`, passing JSON on stdin. It appends one line to `~/.claude/obsidian-brain-ask-log.jsonl`. It follows `summarizer_metrics.py`: local only, rotates at 1 MB, and never raises into the skill.

Each line holds:

| Field | Content |
|---|---|
| `ts`, `session_id`, `project` | when, which session, which project |
| `question` | the question as asked |
| `candidates[]` | every candidate from Steps 3–4: `path`, `fts_rank`, `rerank_score`, `rule_score`, `read` (bool) |
| `cited[]` | the note filenames linked in the answer's Sources section |
| `ranker_source` | `current` in phase 1 |

**Labels from the log:**

- cited → relevant
- read but not cited → weakly not relevant
- never read → no label

Unread candidates are not treated as negatives. That would bias the eval toward the current ranking, since the current ranking decides what gets read.

### 2. Seed eval set: `scripts/rerank_eval_seed.py`

The citation log starts empty, so a seed set provides labels from day one:

1. Collect past `/vault-ask` questions from the session notes. Look for `/vault-ask` or `obsidian-brain:vault-ask` invocations in the raw conversation sections. Target 40–60 distinct questions.
2. Re-run today's retrieval (`search_vault` plus the Step 5 rules) for each question and keep the top 20 candidates.
3. Opus labels each question and candidate pair from 0 to 2: 0 = not relevant, 1 = related, 2 = answers the question. It uses `claude -p --model opus` with the question plus the note's title, summary and first 60 lines.
4. A person spot-checks 20 labels. If agreement is below 85%, the seed set is marked INCONCLUSIVE and the prompt is fixed before any backend is scored. This is the same rule as phase A.

The seed set is written to `~/.claude/obsidian-brain-rerank-seed.jsonl`. It is never committed, because it contains vault text.

### 3. Eval harness: `scripts/rerank_eval.py`

One interface:

```python
def score(question: str, candidates: list[Candidate]) -> list[float]:
    """Return p(relevant) per candidate, same order."""
```

Backends:

| Backend | How | Notes |
|---|---|---|
| `current` | `rerank_results` plus the Step 5 rule table | the baseline |
| `haiku` | one `claude -p --model haiku` call per question, returning JSON scores | subscription, no API key |
| `jev` | HTTP to `TYPESAFE_BASE_URL` with model pinned to `jev-1.13.0`; one `Score` question per candidate, all in one call | **opt-in** with `OB_JEV=1`; text goes through the cc-token-router phase A scrubber first; key from `TYPESAFE_API_KEY` |
| `laya` | subprocess to a separate Python ≥ 3.10 venv (`~/.cache/obsidian-brain/laya-venv`) running a small `laya_score.py` over stdin/stdout JSON; checkpoint `laya` (English) | local, no key; snippets trimmed to fit 512 tokens |

The plugin must keep Python 3.9 working (`tests/test_py39_compat.py`). So laya is never imported by plugin code. Only the harness calls it, as a separate process.

**Question-type test.** The same harness scores a question-type Choice: past decision / error or debugging / how-to or general / status. It compares a keyword rule, `jev` and `laya` against Opus labels. It also measures the nDCG@5 change when the chosen type feeds `search_vault`'s `note_types` weights. #376's missing types (`claude-snapshot`, `claude-stats`, `claude-emerge`, `claude-memory`) get weights as part of this work.

## Metrics

Per backend, first on the seed set, then on the citation log once it has at least 100 questions:

- **nDCG@5** (primary). vault-ask reads about 5–10 notes, so the order of the first five matters most.
- **Recall@5** of notes labelled 2 (seed set) or cited (log).
- **p95 latency** to score 20 candidates.
- **Cost** per question.
- Question type: accuracy against Opus labels, and the nDCG@5 change.

## Go gate for phase 2

A backend passes only if all three hold, each judged with a 95% bootstrap CI:

1. nDCG@5 is at least **+0.05** over `current`, with the lower CI bound above 0.
2. Recall@5 is **no worse** than `current`.
3. p95 latency is at most **1.5 s** for 20 candidates.

Ties go to the cheaper and more private backend, in this order: `current` → `laya` → `jev` → `haiku`.

If nothing passes, phase 2 is dropped and the result is recorded, as with cc-token-router#143.

**Fine-tuning laya** starts only if zero-shot laya fails the gate, Jev passes it, and the cloud route is rejected. The citation log is then the training set. Training runs locally, or on Kaggle's free GPUs (the README's notebook) with the upload explicitly accepted.

## Phase 2 outline (not built here)

- vault-ask Step 5 calls the winning backend to **re-order** the top 20 FTS candidates. It never adds or removes any.
- It falls back to `current` on a 2 s timeout or any error, and records `ranker_source` in the citation log.
- `search_vault`, `/recall` and `/compress` are unchanged.

## Privacy

- The citation log and the seed set stay under `~/.claude/` and are never committed.
- `jev` runs only with `OB_JEV=1`, sends only scrubbed title, summary and snippet text, and never sends whole notes. Read TypeSafe's data policy before the first run.
- `laya` runs fully on the Mac.

## Error handling

| Failure | Behaviour |
|---|---|
| `ask_log.py` fails | vault-ask carries on. The error goes to the hook log; no answer is lost. |
| A backend times out or errors in the harness | That question is recorded as a failure for that backend and counts against it. The run continues. |
| laya venv missing | The harness skips `laya` and says how to create the venv. |
| `OB_JEV` unset or no key | The harness skips `jev` and says why. |

## Testing

- `ask_log.py`: pytest cases for the rotation boundary, a Sources section with no citations, wikilinks that match no candidate, and a malformed stdin payload. It is covered by the 90% gate on `hooks/`.
- `rerank_eval.py`: backends faked with `monkeypatch`, the same way `subprocess.run` is faked elsewhere. nDCG@5, recall@5 and the bootstrap are checked against hand-computed examples.
- `test_py39_compat.py` stays green. No laya import anywhere in `hooks/`.

## Out of scope for this spec

- Phase 2 reranking, which gets its own spec after the gate.
- laya fine-tuning.
- check-items, `memory_inject.py` and grounding checks.
