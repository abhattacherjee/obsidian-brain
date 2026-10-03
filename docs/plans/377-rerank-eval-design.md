# Rerank eval: design

- **Issue:** abhattacherjee/obsidian-brain#377
- **Date:** 2026-09-28
- **Status:** approved 2026-09-28
- **Related:** #376 (missing note types in `_TYPE_SCORES_BY_CONTEXT`), #54 (retrieval refinements)

## Goal

Improve **recall quality**: get the notes that answer a question into context more often.

Before changing any ranking, measure whether a decision model does better than today's ranking. The candidates are Jev (TypeSafe's cloud System One model) and laya (an open, local model with the same interface).

**Phase 1 (this spec) changes no ranking weights.** The one ordering change is that vault-ask's Step 5 rule table moves from the model into a script (see Citation log). Phase 1 adds a citation log, a seed eval set and an eval harness. Phase 2, which reranks vault-ask candidates, has its own spec and only happens if a backend passes the go gate below.

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
- Any change to the default ranking of `search_vault`, `/recall` or `/compress`. The new `search_vault` parameters (`log_access`, `task_context`) and the `rerank_results` extra signal default to today's behaviour.

## Components

### 1. Citation log: `hooks/ask_log.py`

The candidate list is not passed by the model, which could drop entries. Instead:

1. Every candidate goes to `~/.claude/obsidian-brain-ask-pending/<session_id>.json`, whichever path produced it. On the fast path, Step 3's search call writes each candidate's path, FTS rank and rerank score. On the fallback path (Step 3 returned fewer than 5 results), Step 4's deduplicated Grep union is written by the same script, with `fts_rank` and `rerank_score` set to null.
2. Step 5's rule scoring moves from the model into a script (`ask_rank.py`). It applies the same rule table, sorts by rule score, breaks ties by `rerank_score` (then path), and adds `rule_score` and `read` to the pending file. This is a behaviour change: today the model applies the table by hand, so the order can differ from what the model would have picked. The rules themselves do not change, and this script is the `current` baseline in the harness.
3. A new last step calls `ask_log.py record --session <id>` and pipes in only the answer text.
4. `ask_log.py` parses the cited wikilinks from the answer's Sources section, merges them with the pending file, appends the line and deletes the pending file.

This matches the note-writer pattern: the model passes text, and code builds the record. It appends one line to `~/.claude/obsidian-brain-ask-log.jsonl`. It follows `summarizer_metrics.py`: local only, rotates at 1 MB, and never raises into the skill.

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

1. **Questions, from two sources:**
   - **History.** Past `/vault-ask` questions from the session notes' raw conversation sections. A rough count on 2026-09-28 found only about 11 distinct questions, though 73 session notes mention vault-ask.
   - **Known-item questions.** Sample notes across types, projects and ages. For each, Opus writes one question that the note answers, the way you would ask it months later without the note's exact words. The source note is a known relevant result. Target 50, for a total of about 60 questions.
2. Re-run today's retrieval (`search_vault` plus the Step 5 rules) for each question with **`log_access=False`** (see Friston below) and keep the top 20 candidates. If a known-item question's source note isn't in the top 20, it is still recorded, which counts as a recall miss for every backend.
3. Opus labels each question and candidate pair from 0 to 2: 0 = not relevant, 1 = related, 2 = answers the question. It uses `claude -p --model opus` with the question plus the note's title, summary and first 60 lines. In the same call, Opus also labels the **question type** (past decision / error or debugging / how-to or general / status), which the question-type test needs.
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
| `current` | the `ask_rank.py` script: Step 5 rule score, ties broken by `rerank_score` | the baseline |
| `haiku` | one `claude -p --model haiku` call per question, returning JSON scores | subscription, no API key; a quality reference only (see Go gate) |
| `jev` | HTTP to `TYPESAFE_BASE_URL` with model pinned to `jev-1.13.0`; one yes/no (`noul`) question per candidate, all in one call; its answer is p(relevant) | **opt-in** with `OB_JEV=1`; text goes through the cc-token-router phase A scrubber first; key from `TYPESAFE_API_KEY` |
| `laya` | subprocess to a separate Python ≥ 3.10 venv (`~/.cache/obsidian-brain/laya-venv`) running a small `laya_score.py` over stdin/stdout JSON; checkpoint `laya` (English) | local, no key; snippets trimmed to fit 512 tokens |

The plugin must keep Python 3.9 working (`tests/test_py39_compat.py`). So laya is never imported by plugin code. Only the harness calls it, as a separate process.

**Question-type test.** The same harness scores a question-type Choice: past decision / error or debugging / how-to or general / status. It compares a keyword rule, `jev` and `laya` against Opus labels. It also measures the nDCG@5 change when the chosen type sets the note-type weights.

Today the only route to those weights is `caller` → `detect_task_context`. That route has five contexts (`debugging`, `standup`, `emerge`, `search`, `general`), not the four question types. It also returns `debugging` on any `fix/*`, `bug/*` or `hotfix/*` branch, so a harness run would depend on the branch it runs from. This spec adds a `task_context: str | None = None` parameter to `search_vault`. When set, it is used as is and `detect_task_context` is not called. The question types map to contexts like this: past decision → `search`, error or debugging → `debugging`, how-to or general → `general`, status → `standup`. #376's missing types (`claude-snapshot`, `claude-stats`, `claude-emerge`, `claude-memory`) get weights in every context row as part of this work. The harness always passes `task_context` (default `search`), so results never depend on the git branch.

## Metrics

Per backend, on the seed set. The citation log is also scored once it has at least 100 questions, but only to watch trends. **It never decides the gate.** Only notes the current ranking put near the top get read, so only they can be cited, and the log favours `current`.

- **nDCG@5** (primary). vault-ask reads about 5–10 notes, so the order of the first five matters most.
- **Recall@5** of notes labelled 2 (seed set) or cited (log).
- **p95 latency** to score 20 candidates.
- **Cost** per question.
- Question type: accuracy against Opus labels, and the nDCG@5 change.

## Go gate for phase 2

A backend passes only if all three hold **on the seed set**, each judged with a 95% bootstrap CI:

1. nDCG@5 is at least **+0.05** over `current`, with the lower CI bound above 0.
2. Recall@5 is **no worse** than `current`: the lower 95% CI bound of the paired difference (backend − `current`) is at least **−0.02**.
3. p95 latency is at most **1.5 s** for 20 candidates.

`haiku` cannot meet the latency bound, because `claude -p` takes several seconds just to start. It is scored as a quality reference and can never be chosen.

Ties go to the cheaper and more private backend, in this order: `current` → `laya` → `jev` → `haiku`.

If nothing passes, phase 2 is dropped and the result is recorded, as with cc-token-router#143.

**Fine-tuning laya** starts only if zero-shot laya fails the gate, Jev passes it, and the cloud route is rejected. The citation log is then the training set. Training runs locally, or on Kaggle's free GPUs (the README's notebook) with the upload explicitly accepted.

## Relationship to the Friston memory layer

The Friston layer and a decision model answer different questions, so they combine rather than compete.

| | Friston layer (today) | Decision model (Jev or laya) |
|---|---|---|
| Asks | Which notes matter to me in general? | Does this note answer this question? |
| Signals | ACT-R activation from `access_log` (0.20 weight), recency, importance, note type, themes and `surprise` | a relevance probability per candidate |
| Query-aware | only lexically: BM25 (0.20) and proximity (0.25) | yes, by meaning |

`rerank_results` has no semantic signal today. The decision model adds that signal; the Friston priors stay.

**What this spec does about it:**

1. **Two ways to use a model score.** The harness scores each model in two modes:
   - `replace`: order by the model's probability alone.
   - `blend`: add the probability to `rerank_results` as an eighth signal. The existing seven signals are scaled down to make room, and the model's weight is swept (0.2, 0.35, 0.5).

   To compute `blend`, `rerank_results` gains an optional `extra_signal: dict[str, float] | None = None` (path → score) and `extra_weight: float = 0.0`. When `extra_weight` is 0, the output is unchanged; a test pins this. Phase 1 never passes a non-zero weight outside the harness, so live ranking is unchanged.

   The gate applies to the best mode. If `blend` wins, activation, recency and importance keep a say, and a model error is damped.
2. **Protect activation from the eval.** `search_vault` writes an `access_log` row for every result it returns (`_batch_log_access` at `vault_index.py:1837`; `query_related_notes` does the same at `:2065`, but the harness never calls it), and there is no way to turn that off. Replaying about 60 questions × 20 candidates would inflate the activation of whatever the replays return, and corrupt the prior being compared against. This spec adds a `log_access: bool = True` parameter to `search_vault`. The seed builder and harness always pass `False`. A test checks that `access_log` row counts are unchanged after a harness run.
3. **Surprise stays separate.** #54 (surprise-boosted retrieval) can be scored in the same harness as a `current+surprise` variant once #54 fixes the 0.0-ambiguity in `detect_surprise`. It is not built here.

**Not in this spec, but made possible by it:**

- **A cleaner activation signal.** Today every returned result counts as an access, so notes that are shown get stronger whether or not they were useful. The citation log records which notes were actually cited. Weighting ACT-R by cited accesses is a change to the Friston layer and needs its own design.
- **Clustering.** Themes come from TF-IDF cosine (`cluster_vectors` at 0.5, `assign_to_theme` at 0.3), and names come from Haiku. A decision model could assign each new note to a theme with a Choice over theme names plus "new". That is about vault structure, not recall, so it is out of scope. The same harness could measure it later.

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
- `search_vault(..., log_access=False)` writes no `access_log` rows, and the default still does.
- A harness run leaves the `access_log` row count unchanged.
- `search_vault(..., task_context=X)` uses X on a `fix/*` branch; the default still calls `detect_task_context`.
- `rerank_results` with `extra_weight=0` returns the same order and scores as before.
- `ask_rank.py`: the rule table, the tie-break, and the fallback path with null `fts_rank`.

## Out of scope for this spec

- Phase 2 reranking, which gets its own spec after the gate.
- laya fine-tuning.
- check-items, `memory_inject.py` and grounding checks.
