# check_items_prefilter fixtures

Scrubbed reproductions of the `cc-telemetry-dashboard` payload from
~/.claude/obsidian-brain/check-items-xkc1ftv3/ (2026-05-15 empirical
repro for obsidian-brain#173).

## Files

- `active_project_evidence.json` — structural copy of `evidence.json`
  with project-specific identifiers replaced by generic Greek-letter
  placeholders (`alpha_task`, `beta_handler`, `zeta_widget`,
  `eta_pipeline`, `theta_counter`, `iota_pager`, etc.). Preserves the
  completion-zone vs activity-zone partition: only completion-zone
  buckets (merged_prs / closed_issues / changelog_excerpt) carry
  completion verbs and completion-zone vocabulary. The commits bucket
  carries only WIP verbs (scaffold, rename, integrate, split, cover,
  bump, wire, document).

- `active_project_partition.json` — 21 groups with vocabulary cleanly
  separated by zone. Expected partition under the v2.6 zone-aware
  prefilter:
  - 11 WIP groups (g01-g11) -> prefiltered (activity zone overlap,
    no completion verb within 120 chars; no completion-zone token
    match). Uses alpha_task / beta_handler / gamma_parser /
    delta_processor / epsilon_emitter vocabulary absent from
    completion-zone buckets.
  - 6 completion-zone groups (g12-g17) -> routed to sub-agent
    (overlap with merged_prs / closed_issues / changelog / releases
    via zeta_widget / eta_pipeline / theta_counter / iota_pager).
  - 4 ref-bearing groups (g18-g21) -> routed to sub-agent (explicit
    `#N` issue/PR references or `f1e2d3c` / `abc1234` commit-sha).

  Empirical result: `prefiltered == 11`, `subagent == 10`.
  Acceptance criterion in the integration test is `prefiltered >= 10`.

## Vocabulary design

The scrubbing uses two disjoint name pools:

  WIP pool (commits only): alpha_task, beta_handler, gamma_parser,
    delta_processor, epsilon_emitter.

  Completion pool (merged_prs, closed_issues, changelog only):
    zeta_widget, eta_pipeline, theta_counter, iota_pager.

This ensures no WIP group representative shares a content-token with
the completion-zone haystack, producing a clean deterministic
partition without relying on proximity heuristics.

## Scrubbing process

The original payload's text fields (commit subjects, PR titles, issue
bodies, changelog excerpts) were replaced with generic placeholders
that preserve the structural shape (length distribution, presence of
completion verbs in the right buckets) without leaking project
specifics. The on-disk original at
`~/.claude/obsidian-brain/check-items-xkc1ftv3/` is the source of
truth for the empirical regression check in Task 7 of the plan.
