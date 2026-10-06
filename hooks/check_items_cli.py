"""
CLI wrappers for /check-items sub-agent stages.

Keeps SKILL.md free of inline agent prompts. Two entry points:
  - run_semantic_merge: Stage 2b grouping sub-agent.
  - run_classifier:     Stage 4 classification sub-agent (Phase E).

Per spec § New hooks/check_items_cli.py (lines 581-587).
Stdin is capped at 1_000_000 characters (project CLAUDE.md security pattern).
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import sys
import tempfile
from pathlib import Path

# Counts CHARACTERS, not bytes: sys.stdin.read(n) is a character read on a
# text stream, so a multi-byte UTF-8 payload can occupy up to ~4x this in
# bytes. The cap is still a real bound; the NAME just has to say what it
# actually counts (#275). Same constant and same policy as
# hooks/note_writer.py.
STDIN_CAP_CHARS = 1_000_000
SUBAGENT_TIMEOUT_SEC = int(os.environ.get("CHECK_ITEMS_SUBAGENT_TIMEOUT_SEC", "300"))

# Cap groups per classifier sub-agent dispatch. Above this count, run_classifier()
# splits into sequential chunks of <=N so a single Sonnet call stays well under
# SUBAGENT_TIMEOUT_SEC. Override via CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE; values <1
# are clamped to 1.
CLASSIFIER_CHUNK_SIZE = max(
    1, int(os.environ.get("CHECK_ITEMS_CLASSIFIER_CHUNK_SIZE", "25"))
)

# rc 6: the sub-agent exited cleanly but produced no parseable output at all
# (empty file AND empty stdout). Distinct from rc 4 (output present but
# malformed) so operators can tell "sub-agent returned nothing" from
# "sub-agent returned garbage" — see #297 defect 1.
RC_NO_OUTPUT = 6

# Per-chunk retry budget. A chunk that fails twice degrades only its OWN
# groups; the run keeps every completed chunk and still dispatches the
# remaining ones (#297 defect 2).
CHUNK_MAX_ATTEMPTS = 2


_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?")
_FENCE_CLOSE_RE = re.compile(r"\n?\s*```\s*$")


def _strip_json_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences (```json … ```) from sub-agent
    stdout output. Some models (notably Haiku) wrap JSON responses in fences
    even when prompted to write raw JSON; the stdout-fallback path must
    tolerate this before json.loads.

    R12 dogfood finding: 52-group obsidian-brain payload, Haiku semantic-merge
    consistently emits fenced output AND skips the output_path write, so the
    fallback's json.loads(cp.stdout) crashed on the leading backtick."""
    if not text:
        return text
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = _FENCE_OPEN_RE.sub("", stripped, count=1)
    stripped = _FENCE_CLOSE_RE.sub("", stripped, count=1)
    return stripped.strip()


_REQUIRED_CLASSIFIER_FIELDS = {
    "group_id", "classification", "confidence",
    "canonical_text", "evidence_citation", "action_required",
}
_VALID_CLASSIFICATIONS = frozenset({"DONE", "NEEDS-ACTION", "STALE", "ACTIVE", "REVIEW"})


def _validate_classifier_payload(parsed) -> bool:
    """Return True iff parsed is a list of dicts with all required classifier
    fields and a recognised classification value.

    Mirrors the checks in open_item_dedup._validate_classifier_response so the
    stdout-fallback path in run_classifier() rejects wrong-shape sub-agent
    responses before they are written to disk.
    """
    if not isinstance(parsed, list):
        return False
    for item in parsed:
        if not isinstance(item, dict):
            return False
        if not _REQUIRED_CLASSIFIER_FIELDS.issubset(item.keys()):
            return False
        if item.get("classification") not in _VALID_CLASSIFICATIONS:
            return False
        if not isinstance(item.get("group_id"), str) or not item["group_id"]:
            return False
    return True


SEMANTIC_MERGE_PROMPT = """You are the semantic-merge sub-agent for an open-items pipeline. The inline JSON below contains N coarse token-grouped open items.

## Your job

Identify groups that describe the same concrete action even when they
share few tokens. Token-based grouping already caught literal
duplicates; your job is to catch semantic duplicates.

## The key test - are these the same action?

Two items A and B should merge when a user would mark BOTH as done
simultaneously once the underlying work ships. If completing A leaves
B still genuinely to-do, they are NOT the same action.

## Concrete examples from real vault data

### SHOULD MERGE (same action, different phrasing)

Example 1:
- A: "Decide text-fallback routing vs. sentinel option to satisfy
     operator choice requires at least two options"
- B: "Review fuzzy-matched cascade candidate about routing N=1 to
     text-fallback"
-> MERGE. Both describe the same decision about N=1 text-fallback
  routing. B is just pointing at a prior note that raised it.

Example 2:
- A: "Fresh session: execute Phases 1-4 (live /compact x2 + Ctrl-D)"
- B: "Complete live-CC smoke test from DEV-TEST-SNAPSHOTS.md"
-> MERGE. Both describe running the smoke-test protocol; the first
  literally lists the steps, the second names it.

### SHOULD NOT MERGE (related but distinct)

Example 3:
- A: "Run /vault-doctor --check snapshot-integrity (dry-run)"
- B: "Run /vault-doctor fix --check snapshot-integrity (apply mode)"
-> DO NOT MERGE. Different commands with different side effects.

Example 4:
- A: "Investigate dispatcher-discovery fallback logic"
- B: "Fix dispatcher-discovery fallback to probe check availability"
-> DO NOT MERGE. Investigate is not Fix.

Example 5:
- A: "PR #67 (doc): Finalize snapshot-summary user-facing docs"
- B: "PR #70 (read-path): Fix session->snapshots forward backlink
     undercounting"
-> DO NOT MERGE. Same parent feature, different PRs shipping different
  work.

## Rules

1. Same-project only (enforced in Python, not by you).
2. Apply the "both get marked done simultaneously" test above.
3. Emit a mergeable pair even when tokens do not overlap - that is the
   whole reason you exist.
4. When in doubt between merging and not, DO NOT MERGE. Classifier
   downstream can still close items separately.

## Output format

Return STRICT JSON ONLY - no prose, no markdown fences, nothing
outside the JSON.

{
  "merges": [
    {
      "canonical_group_id": "ob-NNNN",
      "absorbed_group_ids": ["ob-MMMM"],
      "reasoning": "one sentence with the both-done-together justification"
    }
  ],
  "total_groups_before": <int>,
  "total_groups_after": <int>
}

Your final message must be exactly the JSON.
"""


def _pick_model(group_count: int) -> str:
    """<=60 groups -> haiku, >60 -> sonnet. Per spec line 83."""
    return "haiku" if group_count <= 60 else "sonnet"


def _read_stdin_capped() -> str:
    """Read stdin with the 1_000_000-character cap (project security pattern)."""
    return sys.stdin.read(STDIN_CAP_CHARS)


def _safe_workdir() -> Path:
    """Return selected private state, with the named legacy compatibility seam."""
    from runtime_adapters import private_workdir
    workdir = private_workdir()
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return workdir


def _publish_private_json(output_path, value):
    """Publish validated private output without exposing a path to the model."""
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is None:
        raise ValueError("Private output requires the invoking host context")
    import stat
    if context.state_path.resolve().is_relative_to(context.vault_path.resolve()):
        raise ValueError("Private result state must remain outside the vault")
    from note_transactions import session_state_path
    jobs = session_state_path(context) / "jobs"
    destination = Path(output_path)
    if destination.suffix != ".json":
        raise ValueError("Private result must be JSON")
    destination.absolute().relative_to(jobs.absolute())
    relative = destination.resolve().relative_to(jobs.resolve())
    for directory in destination.absolute().parents:
        if directory == jobs.absolute():
            break
        if directory.is_symlink():
            raise ValueError("Private result directory cannot be a symlink")
    if len(relative.parts) < 2 or re.fullmatch(r"[0-9a-f]{32}", relative.parts[0]) is None:
        raise ValueError("Private result requires a jobs operation ID")
    directory = jobs
    for part in relative.parts[:-1]:
        directory = directory / part
        details = directory.lstat()
        if (directory.is_symlink() or not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700):
            raise ValueError("Private result directory is unsafe")
    if destination.is_symlink():
        raise ValueError("Private result cannot be a symlink")
    if destination.exists():
        details = destination.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600):
            raise ValueError("Existing private result is unsafe")
    descriptor, temporary = tempfile.mkstemp(dir=str(destination.parent), prefix=".check-items-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _request_ai(operation, prompt, payload, model, groups):
    from ai_backend import AIRequest, execute_ai
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is None:
        print("[check-items-cli] invoking host context required", file=sys.stderr)
        return 2, None
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > STDIN_CAP_CHARS:
        return 2, None
    ids = [group["group_id"] for group in groups]
    selected_model = model if context.host == "claude" else None
    if context.host == "claude" and operation == "classify_items":
        explicit = context.config.get("classifier_model")
        if isinstance(explicit, str) and explicit.startswith("claude-"):
            selected_model = explicit
    request = AIRequest(
        input=prompt + "\n\nInput JSON:\n" + serialized,
        input_revision=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        timeout=SUBAGENT_TIMEOUT_SEC,
        model=selected_model,
        options={"expected_ids": ids,
                 "project_by_id": {group["group_id"]: group.get("project", "") for group in groups},
                 "expected_count": len(groups)},
    )
    result = execute_ai(context, operation, request)
    if result.status != "ok":
        print(f"[check-items-cli] {operation}: {result.status}: {result.diagnostic}", file=sys.stderr)
        if result.status == "unavailable" and result.error_code == "native_execution_failed":
            return 3, result
        return {"timeout": 3, "invalid_output": 4, "cancelled": 7, "unavailable": 8, "auth_error": 8}.get(result.status, 4), result
    if result.input_revision != request.input_revision:
        return 4, result
    return 0, result


def _valid_groups(groups):
    return (isinstance(groups, list)
            and all(isinstance(group, dict) and isinstance(group.get("group_id"), str)
                    and group["group_id"] for group in groups)
            and len({group["group_id"] for group in groups}) == len(groups))


def _validate_merge_payload(value, groups):
    if not isinstance(value, dict) or not isinstance(value.get("merges"), list):
        return False
    projects = {group["group_id"]: group.get("project", "") for group in groups}
    absorbed = set()
    canonicals = set()
    for merge in value["merges"]:
        if not isinstance(merge, dict):
            return False
        canonical = merge.get("canonical_group_id")
        members = merge.get("absorbed_group_ids")
        if (not isinstance(canonical, str) or canonical not in projects
                or not isinstance(members, list) or not members
                or not isinstance(merge.get("reasoning"), str)):
            return False
        if canonical in canonicals or canonical in absorbed:
            return False
        canonicals.add(canonical)
        for member in members:
            if (not isinstance(member, str) or member not in projects or member == canonical
                    or member in absorbed or member in canonicals or projects[member] != projects[canonical]):
                return False
            absorbed.add(member)
    return (not canonicals.intersection(absorbed)
            and type(value.get("total_groups_before")) is int
            and type(value.get("total_groups_after")) is int
            and value["total_groups_before"] == len(groups)
            and value["total_groups_after"] == len(groups) - len(absorbed))


def run_semantic_merge(stdin_json: str, output_path: str) -> int:
    try:
        payload = json.loads(stdin_json)
    except json.JSONDecodeError:
        return 2
    if not isinstance(payload, dict) or not _valid_groups(payload.get("groups")):
        return 2
    groups = payload["groups"]
    rc, result = _request_ai("semantic_merge", SEMANTIC_MERGE_PROMPT, payload, _pick_model(len(groups)), groups)
    if rc:
        return rc
    if not _validate_merge_payload(result.data, groups):
        return 4
    _publish_private_json(output_path, result.data)
    return 0


# Stage 4 classifier prompt. Spec §§:
#   - Classifier contract / Prompt shape (lines 262-289)
#   - Classification semantics (lines 317-322) — INCLUDING the self-referential
#     rule (line 321, Patch 3): discovery evidence is NOT completion evidence.
CLASSIFIER_PROMPT = """You are the classifier sub-agent for /check-items. The inline JSON below contains:
  - groups: list of merged open-item groups (post Stage 2b).
  - evidence: per-project bundle (commits, merged_prs, closed_issues,
    releases, changelog_excerpt, fts_mentions, note_completions).

## Your job

For each group, decide whether the action it describes is DONE,
NEEDS-ACTION, STALE, ACTIVE, or REVIEW. Cite the specific evidence you used.

## Classification semantics

- DONE — the action is complete. Cite at least one of: merged PR title,
  commit sha, closed issue body, release note, or an insight note that
  explicitly marks the item done.
  A `note_completions` entry is a valid DONE citation for a project with no
  git repo: it means a STRICTLY NEWER session summary reports the item done.
  Each entry carries a `contradicted_by` date (YYYY-MM-DD) and a
  `contradicted_by_title` (the newer session's title/first summary line).
  Build the citation from EXACTLY those two fields:
  `reported done in session <contradicted_by> (<contradicted_by_title>)`
  — this is the ONLY shape the tier rules recognise as a MED-tier
  note-completion citation; anything else reads as a plain citation with
  no special handling. (The guarantee that this source never reaches HIGH
  is enforced in code from the evidence bundle's own shape, not from your
  wording — do not rely on phrasing to keep it capped.)
- NEEDS-ACTION — the fix is shipped, but the literal action is an
  external command this tool cannot run (e.g. `gh issue close`, token
  rotation, manual verification). Set `action_required` to a
  copy-pasteable command or instruction.
- STALE — item is >90 days old and no recent evidence mentions it.
  LOW confidence by default.
- ACTIVE — you did not find sufficient evidence to close. Set
  `evidence_citation` to null.
- REVIEW — the item names a shipped-looking component, feature, or
  branch, but you found no citable issue/PR/commit/title to confirm
  completion. You are uncertain. Surface it for the user to judge; do
  NOT guess DONE and do NOT bury it as ACTIVE. LOW confidence. Set
  evidence_citation to the closest weak signal (e.g. a similarly-named
  branch/tag/path) or null.

## Self-referential evidence rule (CRITICAL)

If the cited evidence is a discovery or description of the bug rather
than its fix-merge (closed PR/issue or commit sha), prefer ACTIVE.
Discovery evidence is NOT completion evidence. Examples:

- Item: "Fix dispatcher-discovery fallback to probe check availability"
- Evidence: "Note 2026-04-22 describes the dispatcher-discovery bug."
- -> ACTIVE. The note describes the bug; it does not ship the fix.

- Item: "Close GitHub issue #534"
- Evidence: "PR #534 merged as abc1234 on 2026-04-24."
- -> DONE. The fix-merge is cited directly.

Precedence vs REVIEW: "prefer ACTIVE" above is the default for items that
name nothing shipped-looking. If the item NAMES a shipped-looking
component, feature, or branch (see REVIEW above) and the only evidence is
self-referential/discovery-only — you cannot confirm completion — prefer
REVIEW instead of ACTIVE. Do not bury it as ACTIVE just because the
evidence is discovery-only.

## Anti-conflation rule

Different PR numbers under the same parent feature ship different work.
If the item references PR #N and the only evidence is PR #M (M != N),
do NOT mark DONE. Prefer ACTIVE unless there is independent evidence
that #N itself merged.

## Output format

Return STRICT JSON ONLY - no prose, no markdown fences.

{"items": [
  {
    "group_id": "ob-NNNN",
    "classification": "DONE | NEEDS-ACTION | STALE | ACTIVE | REVIEW",
    "confidence": "HIGH | MED | LOW",
    "canonical_text": "<short canonical phrasing of the action>",
    "evidence_citation": "<specific commit sha / PR# / issue# / release / note ref, OR null>",
    "action_required": "<command string for NEEDS-ACTION, else null>"
  }
]}

Your final message must be exactly the JSON object.
"""


def _pick_classifier_model(group_count: int) -> str:
    """<=30 merged groups -> haiku; >30 -> sonnet. Per spec line 106."""
    return "haiku" if group_count <= 30 else "sonnet"


def _strip_unreleased_section(changelog: str) -> str:
    """Remove the `## [Unreleased]` section (up to the next `## [x.y.z]` heading
    or end of text) from a Keep-a-Changelog excerpt. Released sections are
    completion statements by construction; the Unreleased section is WIP and
    over-triggers Rule 2 of has_classifiable_evidence.

    Case-insensitive match on `## [Unreleased]` (with optional trailing
    whitespace). If no Unreleased section is present, returns the input
    unchanged.
    """
    if not changelog:
        return ""
    # Match "## [Unreleased]" through the start of the next "## [" version heading
    # OR end-of-string. DOTALL so '.' spans newlines.
    pattern = re.compile(
        r"##\s*\[Unreleased\][^\n]*\n.*?(?=^##\s*\[|\Z)",
        flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    return pattern.sub("", changelog)


# H1 (#318 hardening pass): an ALLOWLIST of git-derived keys, read directly
# off deep_analysis_pipeline's proj_evidence[...] assignment sites
# (open_item_dedup.py) rather than trusting a prior draft. An open-ended
# "any other key counts as git evidence" test (the pre-H1 shape) is right
# for a future git-derived source and wrong for a future non-git one — and
# #318 exists precisely to add non-git sources, so the next one is more
# likely non-git than git. A key outside this set must be classified
# deliberately; it does NOT default into "this project has real evidence,
# uncap it".
_GIT_DERIVED_EVIDENCE_KEYS = frozenset({
    "commits", "tags", "changed_paths", "releases",
    "merged_prs", "closed_issues", "changelog_excerpt", "fts_mentions",
})


def _is_empty(v) -> bool:
    """Empty for a falsy value, and for a dict or list whose entries are
    all falsy (H2, #318 hardening pass).

    fts_mentions is a dict of hit COUNTS, so `{"x": 0}` is a truthy dict
    meaning "searched, found nothing" -- a bare `not v` reads that as real
    evidence. Same shape risk applies to any future evidence bucket built
    the same way (a mapping or list of zero/empty results), so this is
    written as a general emptiness check, not an fts_mentions special case.
    """
    if not v:
        return True
    if isinstance(v, dict):
        return not any(v.values())
    if isinstance(v, list):
        return not any(v)
    return False


def note_evidence_only_for(evidence: dict, project: str) -> bool:
    """True when a project's ONLY real evidence is note_completions.

    The enforcing half of #318's MED cap (F7): if the only thing backing a
    project is note_completions, no verdict for it can legitimately be
    HIGH, whatever the classifier wrote — assign_tier's note_evidence_only
    parameter caps at MED unconditionally when this is True, independent of
    citation wording (the fix-round-2 CRITICAL: a text-shape regex alone is
    not enforcement against a model's own prose).

    Extracted to module level (F12, #318 fix round 4) so BOTH producers of
    a classification record use the identical predicate and cannot drift
    apart: run_classifier's per-project stamp (fresh sub-agent + L2
    synthetic verdicts) and skills/check-items/SKILL.md Step 6's
    cache-merge block (replayed verdicts for known_unchanged groups) — the
    second of which had NO note_evidence_only key at all until F12, so a
    cached DONE citation sharing a literal ref with the item text could
    reach HIGH and get auto-checked on every re-run after the first.

    VALUE-aware, not key-presence, for the git-derived buckets (F13, #318
    fix round 4 addendum): deep_analysis_pipeline's git block sets
    tags/changed_paths/merged_prs/closed_issues to `[]` on EVERY failure
    branch (no tags, gh unauthenticated, etc. — the normal state on a
    fresh machine or in CI), so a repo-backed project with zero usable git
    evidence still carries those keys. Checking VALUES (via `_is_empty`),
    not key presence, treats `{"tags": [], "note_completions": [...]}` the
    same as `{"note_completions": [...]}` — correctly, since an empty list
    proves nothing either way.

    #318 hardening pass, on top of F12/F13:
    - H1: git evidence is judged against `_GIT_DERIVED_EVIDENCE_KEYS`, an
      explicit allowlist, not "any key other than note_completions".
    - H2: `_is_empty` treats a dict/list of all-falsy entries as empty too,
      not just a falsy container itself (the fts_mentions {"x": 0} case).
    - H3: the gate on `note_completions` checks PRESENCE before emptiness
      — distinguishes "this bundle never attempted a note-completion scan
      for this project" (key absent) from "the scan ran and found
      nothing" (key present, empty). Both read False today, but they are
      different situations, and collapsing them into one truthiness check
      is exactly the kind of normalization that would silently change
      behaviour if deep_analysis_pipeline is ever edited to assign
      note_completions unconditionally, the way its git-derived siblings
      already are.
    - H4: an unresolvable project (no name to look evidence up by at all —
      `g.get("project", "")` defaults to `""` when a group carries no
      `project` field) fails SAFE: cap it, don't uncap it, because we
      cannot justify HIGH for a citation whose project we cannot even
      identify. This is narrower than "the project name is valid but
      produced no evidence" (an ordinary evidence-less project, e.g.
      project="notes-only" simply absent from `evidence`), which stays
      False — #297's `_cap_at_med` already caps that case via the
      synthetic/heuristic path regardless of this flag.
    """
    if not project:
        return True

    proj = evidence.get(project) or {}
    if "note_completions" not in proj:
        return False
    if _is_empty(proj["note_completions"]):
        return False

    has_git_evidence = any(
        not _is_empty(proj.get(k)) for k in _GIT_DERIVED_EVIDENCE_KEYS
    )
    return not has_git_evidence


def _bridge_project_evidence(evidence: dict, project: str) -> dict:
    """Convert project evidence to the _text-suffixed flat format expected by
    has_classifiable_evidence().

    The live evidence payload from open_item_dedup.py uses a project-keyed nested
    structure with bare keys:
        {project_name: {"commits": [...], "merged_prs": [...], ...}}

    has_classifiable_evidence() expects a flat dict with _text-suffixed keys:
        {"commits_text": "...", "merged_prs_text": "...", ...}

    This helper bridges both shapes:
    - If the evidence dict contains the project key AND its value is a dict
      with bare keys → extract and convert to _text form.
    - If the evidence dict already has _text-suffixed keys at the top level
      (test fixtures, simplified payloads) → return as-is.
    - If neither shape matches → return empty dict (fail-safe: no evidence → synthetic).
    """
    # Shape A: project-nested bare keys (production shape from open_item_dedup.py)
    if project and project in evidence and isinstance(evidence[project], dict):
        proj = evidence[project]
        # Convert list/dict values to text by joining stringified items
        def _to_text(val) -> str:
            if not val:
                return ""
            if isinstance(val, str):
                return val
            if isinstance(val, list):
                parts = []
                for item in val:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        # merged_prs / closed_issues: titles encode completion;
                        # bodies are activity content (discuss problem + adjacent work)
                        # and would over-trigger Rule 2 of has_classifiable_evidence.
                        # Include number for traceability but NOT body.
                        title = item.get("title", "")
                        number = item.get("number", "")
                        if title or number:
                            parts.append(f"#{number} {title}".strip() if number else title)
                        else:
                            # Unknown dict shape — fall back to joining values (preserves
                            # backward-compat for non-{title,body} dicts)
                            parts.append(" ".join(str(v) for v in item.values() if v))
                return " ".join(parts)
            if isinstance(val, dict):
                # fts_mentions: {text: count} — join all keys
                return " ".join(str(k) for k in val)
            return str(val)

        zone_text = {
            "commits_text": _to_text(proj.get("commits")),
            "merged_prs_text": _to_text(proj.get("merged_prs")),
            "closed_issues_text": _to_text(proj.get("closed_issues")),
            "releases_text": _to_text(proj.get("releases")),
            "changelog_excerpt": _strip_unreleased_section(proj.get("changelog_excerpt") or ""),
            "fts_mentions_text": _to_text(proj.get("fts_mentions")),
            # #318: note_completions entries (gather_note_completion_evidence)
            # carry pre-resolved item text, not a bucket to text-join like the
            # zones above -- has_classifiable_evidence's Rule 0 compares each
            # entry directly against a group's canonical text.
            "note_completion_items": [
                r.get("text", "") for r in (proj.get("note_completions") or [])
            ],
        }

        # #264 Task 2 follow-up: fold the bounded/deduped `tags` and
        # `changed_paths` git ground truth (collected by
        # open_item_dedup.deep_analysis_pipeline) into the completion-zone
        # text so has_classifiable_evidence() can see it. Without this, a
        # no-anchor item whose only evidence is a git tag or a changed file
        # path never reaches the sub-agent classifier. Degrades cleanly when
        # `proj` carries no tags/changed_paths keys (helper treats missing as
        # empty).
        from open_item_dedup import fold_tags_and_paths_into_completion_zone
        return fold_tags_and_paths_into_completion_zone(zone_text, proj)

    # Shape B: already _text-suffixed at top level (test fixtures / simplified payloads)
    _TEXT_KEYS = {"commits_text", "merged_prs_text", "closed_issues_text",
                  "releases_text", "changelog_excerpt", "fts_mentions_text",
                  "note_completion_items"}
    if _TEXT_KEYS.intersection(evidence.keys()):
        return evidence

    # Unknown shape or missing project → no evidence (fail-safe).
    # Before warning, check whether the payload IS a valid shape A for a
    # different project (multi-project payload, this group's project not
    # present). Shape A is recognized by any top-level value being a dict
    # that contains at least one bare evidence key. In that case, the
    # legitimate answer is "no evidence for this project" — return {} silently.
    # Only WARN when neither shape A nor shape B is recognizable at all.
    _BARE_KEYS = {"commits", "merged_prs", "closed_issues", "releases", "note_completions",
                  "changelog_excerpt", "fts_mentions"}
    _is_shape_a_payload = any(
        isinstance(v, dict) and _BARE_KEYS.intersection(v.keys())
        for v in evidence.values()
    )
    if _is_shape_a_payload:
        # Valid shape A payload, but project not present — no evidence for this group.
        return {}

    # Genuinely unrecognized shape — warn, as this indicates a format change
    # that would silently STALE all groups without this diagnostic.
    if evidence:
        print(
            f"[check-items-cli] WARN: _bridge_project_evidence unrecognized shape "
            f"for project={project!r}; top-level keys={list(evidence.keys())[:10]}",
            file=sys.stderr,
        )
    return {}


def _dispatch_classifier_chunk(chunk_groups: list, evidence: dict, model: str,
                               output_path: str, chunk_label: str = "") -> tuple[int, list]:
    """Return validated inline results; the model never sees an output path."""
    if not _valid_groups(chunk_groups) or not isinstance(evidence, dict):
        return 2, []
    rc, result = _request_ai("classify_items", CLASSIFIER_PROMPT,
                             {"groups": chunk_groups, "evidence": evidence}, model, chunk_groups)
    if rc:
        return rc, []
    parsed = result.data
    if not _validate_classifier_payload(parsed):
        return 4, []
    expected = {group["group_id"] for group in chunk_groups}
    returned = [item["group_id"] for item in parsed]
    if len(returned) != len(expected) or set(returned) != expected:
        return 4, []
    for item in parsed:
        if (item.get("confidence") not in {"HIGH", "MED", "LOW"}
                or not isinstance(item.get("canonical_text"), str)
                or not isinstance(item.get("evidence_citation"), (str, type(None)))
                or not isinstance(item.get("action_required"), (str, type(None)))
                or (item["classification"] != "NEEDS-ACTION" and item["action_required"] is not None)
                or (item["classification"] == "NEEDS-ACTION" and not item["action_required"])):
            return 4, []
    for item in parsed:
        item["ai_backend"] = result.backend
        item["ai_model"] = result.model
        item["ai_prompt_version"] = "check-items-classifier-v3"
        item["ai_prompt_sha256"] = hashlib.sha256(CLASSIFIER_PROMPT.encode("utf-8")).hexdigest()
    return 0, parsed


def run_classifier(stdin_json: str, output_path: str) -> int:
    """
    Stage 4: invoke the classifier sub-agent, with L2 evidence-presence
    pre-filter applied to all groups before native analysis dispatch.

    Flow:
      1. Parse stdin JSON to extract groups + evidence.
      2. Apply L2 pre-filter (if enabled): items with no evidence go to
         synthetic_classification(); items with evidence go to the sub-agent.
      3. If to_classify is non-empty, dispatch native analysis. When
         len(to_classify) > CLASSIFIER_CHUNK_SIZE, split into sequential
         chunks of <=CLASSIFIER_CHUNK_SIZE groups so a single call stays
         well under SUBAGENT_TIMEOUT_SEC.
      4. Merge sub-agent results + synthetic results in input order.
      5. Write merged output to output_path (0o600).
      6. Emit telemetry line to stderr.

    Returns 0 on success, non-zero on failure.
    """
    import time as _time
    _wall_start = _time.monotonic()

    try:
        payload = json.loads(stdin_json)
    except json.JSONDecodeError as exc:
        print(f"[check-items-cli] ERROR: classifier stdin invalid JSON: {exc}",
              file=sys.stderr)
        return 2

    if not isinstance(payload, dict) or not _valid_groups(payload.get("groups")):
        print("[check-items-cli] invalid groups or group_id", file=sys.stderr)
        return 2
    groups = payload["groups"]
    evidence = payload.get("evidence", {})
    if not isinstance(evidence, dict):
        return 2

    # -----------------------------------------------------------------------
    # Validate group_id presence — a None group_id causes silent key collision
    # in merged_by_id, so two groups overwrite each other and the ordered list
    # contains the same record twice.  Fail fast with a clear diagnostic.
    # -----------------------------------------------------------------------
    invalid_ids = [i for i, g in enumerate(groups) if g.get("group_id") is None]
    if invalid_ids:
        print(
            f"[check-items-cli] ERROR: {len(invalid_ids)} group(s) have group_id=None "
            f"(indices {invalid_ids[:10]}); cannot merge — aborting classifier",
            file=sys.stderr,
        )
        return 5

    # -----------------------------------------------------------------------
    # L2: evidence-presence pre-filter
    # Groups reaching this function are already L1-miss (partition() in the
    # SKILL.md orchestration layer handled cache hits before calling the CLI).
    # -----------------------------------------------------------------------
    from check_items_prefilter import (
        has_classifiable_evidence,
        synthetic_classification,
        is_prefilter_enabled,
    )

    synthetic: list = []
    to_classify: list = list(groups)

    if is_prefilter_enabled() and groups:
        to_classify = []
        for g in groups:
            project = g.get("project", "")
            bridged_evidence = _bridge_project_evidence(evidence, project)
            if has_classifiable_evidence(g, bridged_evidence):
                to_classify.append(g)
            else:
                synthetic.append(synthetic_classification(g))

    # -----------------------------------------------------------------------
    # Sub-agent dispatch — only on groups that have evidence
    # -----------------------------------------------------------------------
    sub_results: list = []
    chunk_count = 0
    failed_chunks = 0
    last_failure = 4
    unclassified_groups = 0

    if to_classify:
        chunks = [to_classify[index:index + CLASSIFIER_CHUNK_SIZE]
                  for index in range(0, len(to_classify), CLASSIFIER_CHUNK_SIZE)]
        chunk_count = len(chunks)
        if chunk_count > 1:
            print(f"[check-items-cli] classifier: chunking {len(to_classify)} groups into {chunk_count} chunk(s)", file=sys.stderr)
        for index, chunk in enumerate(chunks):
            model = _pick_classifier_model(len(chunk))
            for attempt in range(1, CHUNK_MAX_ATTEMPTS + 1):
                rc, chunk_results = _dispatch_classifier_chunk(
                    chunk, evidence, model, output_path,
                    chunk_label=f"chunk {index + 1}/{chunk_count}, attempt {attempt}: ",
                )
                if rc in {7, 8}:
                    return rc
                if rc == 0:
                    break
            if rc != 0:
                last_failure = rc
                failed_chunks += 1
                unclassified_groups += len(chunk)
                continue
            sub_results.extend(chunk_results)

    # -----------------------------------------------------------------------
    # Merge in input order and write final output
    # -----------------------------------------------------------------------
    merged_by_id: dict = {}
    for s in sub_results:
        merged_by_id[s["group_id"]] = s
    for s in synthetic:
        merged_by_id[s["group_id"]] = s

    # -------------------------------------------------------------------
    # F7 (#318 fix round 2, CRITICAL): stamp note_evidence_only per project
    # onto every record -- sub-agent AND synthetic alike. This is DATA WE
    # CONTROL (the evidence bundle's own keys), not text a model composes,
    # so it can't be bypassed by off-template wording the way fix round 1's
    # citation-shape regex could. If a project's evidence bundle contains
    # ONLY note_completions (no git-derived bucket at all), no citation for
    # any item in it can legitimately be HIGH -- Step 7 threads this flag
    # into assign_tier(), which caps at MED unconditionally when it's set,
    # regardless of what the classifier actually wrote. Computed once per
    # project via a project -> bool memo, not once per group.
    # -------------------------------------------------------------------
    _note_only_by_project: dict[str, bool] = {}

    def _note_evidence_only(project: str) -> bool:
        if project not in _note_only_by_project:
            _note_only_by_project[project] = note_evidence_only_for(evidence, project)
        return _note_only_by_project[project]

    for g in groups:
        gid = g.get("group_id")
        if gid in merged_by_id:
            merged_by_id[gid]["note_evidence_only"] = _note_evidence_only(g.get("project", ""))

    ordered = [merged_by_id[g["group_id"]] for g in groups if g["group_id"] in merged_by_id]
    if failed_chunks and not ordered:
        return last_failure

    _publish_private_json(output_path, ordered)

    # -----------------------------------------------------------------------
    # Telemetry — inner line (CLI sees only L1-miss groups; cache_hit is '-'
    # by design — the orchestration layer in open_item_dedup.py holds that
    # count and emits the outer classifier-result line with real cache_hit).
    # -----------------------------------------------------------------------
    _wall = int(_time.monotonic() - _wall_start)
    total = len(groups)
    prefiltered_count = len(synthetic)
    subagent_count = len(sub_results)
    chunks_field = f" chunks={chunk_count}" if chunk_count > 1 else ""
    failed_field = (
        f" failed_chunks={failed_chunks} unclassified={unclassified_groups}"
        if failed_chunks else ""
    )
    print(
        f"[check-items-cli] classifier: total={total} cache_hit=- "
        f"prefiltered={prefiltered_count} subagent={subagent_count}"
        f"{chunks_field}{failed_field} wall={_wall}s",
        file=sys.stderr,
    )

    return 0


def main():
    """CLI entrypoint. Usage: python3 check_items_cli.py <command> <output_path>"""
    if len(sys.argv) < 3:
        print("usage: check_items_cli.py <semantic_merge|classifier> <output_path>",
              file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    output_path = sys.argv[2]
    stdin_json = _read_stdin_capped()
    if cmd == "semantic_merge":
        sys.exit(run_semantic_merge(stdin_json, output_path))
    if cmd == "classifier":
        sys.exit(run_classifier(stdin_json, output_path))
    print(f"unknown command: {cmd}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
