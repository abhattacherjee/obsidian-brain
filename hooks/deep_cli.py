"""CLI helpers for /standup deep — thin wrappers around open_item_dedup functions.

Each function is designed to be called from a minimal ``python3 -c`` stub
in the standup SKILL.md, keeping inline code to 2-3 lines.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import os
import re
import sys
import time

from open_item_dedup import (
    anchor_text_matches,
    build_deep_presentation,
    deep_analysis_pipeline,
)

# Matches an UNCHECKED markdown checkbox line with non-empty item text.
_UNCHECKED_CHECKBOX_RE = re.compile(r"^\s*-\s+\[ \]\s+\S")

# Counts CHARACTERS, not bytes -- same constant and same policy as
# hooks/note_writer.py and hooks/check_items_cli.py (#275).
STDIN_CAP_CHARS = 1_000_000


def _read_stdin_capped() -> str:
    """Read stdin with the project cap, rejecting oversize input.

    Two entry points in this module previously read stdin with an UNBOUNDED
    ``json.load(...)``, which consumes the stream to EOF; the other two used
    the capped ``read()`` idiom. Both unbounded ones are reachable from skill
    invocations, so all four now route through this one capped reader.

    Rejects rather than truncates: a truncated JSON payload would fail to
    parse anyway, but with a confusing decode error instead of a statement
    about the cap.
    """
    text = sys.stdin.read(STDIN_CAP_CHARS + 1)
    if len(text) > STDIN_CAP_CHARS:
        print(
            f"[obsidian-brain] stdin exceeds the {STDIN_CAP_CHARS}-character cap; "
            "nothing processed",
            file=sys.stderr,
        )
        sys.exit(2)
    return text


def _is_checkbox_flip(old_text: str, new_text: str) -> bool:
    """True iff (old_text -> new_text) is a pure unchecked->checked checkbox flip.

    old_text must be an unchecked checkbox line (``- [ ] <text>``) and new_text
    must equal old_text with ONLY the first ``[ ]`` replaced by ``[x]`` — every
    other character identical. Anything else (link additions, prose edits) is
    NOT a checkbox flip and keeps the legacy substring-replace path.
    """
    if not _UNCHECKED_CHECKBOX_RE.match(old_text):
        return False
    flipped = old_text.replace("[ ]", "[x]", 1)
    return flipped == new_text


def run_pipeline(vault_path: str, sessions_folder: str, insights_folder: str, *, operation_id=None) -> None:
    """Run deep analysis pipeline for /standup deep.

    Reads ``{"basenames": [...], "projects": [...]}`` from stdin.
    Prints status line: ``OK:<n>:<g>:<e>`` or ``CACHED:<n>:<g>:<e>``.
    """
    data = json.loads(_read_stdin_capped())
    basenames = data["basenames"]
    projects_json = json.dumps(data["projects"])

    from runtime_context import current_runtime_context
    from operation_state import operation_directory
    context = current_runtime_context()
    identity, directory = operation_directory(context, operation_id)
    if Path(vault_path).resolve() != context.vault_path.resolve():
        raise ValueError('Pipeline cannot switch selected vault')
    output_path = str(directory / 'deep-pipeline.json')
    semantic = _pipeline_identity(vault_path, basenames, data['projects'])

    status = deep_analysis_pipeline(
        basenames,
        projects_json,
        output_path,
        vault_path,
        sessions_folder,
        insights_folder,
        operation_id=identity, operation_semantic=semantic,
    )

    # Filter out recently acted-on items so they aren't re-recommended
    acted = _load_acted_items()
    if acted and not status.startswith("ERROR:") and os.path.isfile(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                pipeline_data = json.load(f)
            groups = pipeline_data.get("items", {}).get("groups", [])
            original_count = len(groups)
            filtered = [g for g in groups if g.get("representative", "") not in acted]
            if len(filtered) < original_count:
                pipeline_data["items"]["groups"] = filtered
                pipeline_data["items"]["group_count"] = len(filtered)
                from operation_state import store_artifact
                store_artifact(context, identity, 'deep-pipeline.json',
                               json.dumps(pipeline_data, indent=2), semantic=semantic)
                skipped = original_count - len(filtered)
                print(f"[obsidian-brain] filtered {skipped} recently acted-on item(s)", file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            pass  # best-effort filtering

    if not status.startswith('ERROR:') and os.path.isfile(output_path):
        from operation_state import store_artifact
        os.chmod(output_path, 0o600)
        store_artifact(context, identity, 'deep-pipeline.json', Path(output_path).read_bytes(), semantic=semantic)
        print(json.dumps({'operation_id': identity, 'operation_dir': str(directory),
                          'pipeline_path': output_path,
                          'classifications_path': str(directory / 'deep-classifications.json')}))
    print(status)


def _pipeline_identity(vault_path, basenames, projects):
    vault = Path(vault_path).resolve()
    sources = {}
    for name in basenames:
        if not isinstance(name, str) or Path(name).name != name or any(c in name for c in '*?[]'):
            raise ValueError('Invalid pipeline source name')
        matches = list(vault.glob('*/' + str(name)))
        for path in matches:
            if path.is_symlink() or vault not in path.resolve().parents:
                raise ValueError('Pipeline source escapes selected vault')
            sources[str(path.relative_to(vault))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'algorithm': 'deep-v1', 'basenames': basenames, 'projects': projects, 'sources': sources}


def run_present(vault_path: str, sessions_folder: str, insights_folder: str, *, operation_id=None, pipeline_path=None, classifications_path=None) -> None:
    """Build deep analysis presentation.

    Reads basenames JSON array from stdin.
    Prints formatted markdown output.
    """
    basenames_json = _read_stdin_capped()
    from runtime_context import current_runtime_context
    from operation_state import operation_directory, operation_semantic, read_artifact
    context = current_runtime_context()
    if operation_id is None:
        raise ValueError('Presentation requires explicit operation identity')
    identity, directory = operation_directory(context, operation_id)
    semantic = operation_semantic(context, identity)
    if semantic != _pipeline_identity(vault_path, semantic['basenames'], semantic['projects']):
        raise ValueError('Pipeline source revision changed')
    read_artifact(context, identity, 'deep-pipeline.json', semantic=semantic, supplied_path=pipeline_path)
    read_artifact(context, identity, 'deep-classifications.json', semantic=semantic, supplied_path=classifications_path)
    output = build_deep_presentation(
        str(directory / 'deep-pipeline.json'),
        str(directory / 'deep-classifications.json'),
        basenames_json,
        vault_path,
        sessions_folder,
        insights_folder,
    )
    print(output)


from runtime_adapters.claude import legacy_private_directory
_ACTED_ITEMS_PATH = str(legacy_private_directory() / "deep-acted-items.json")
_ACTED_TTL_SECONDS = 86400  # 24 hours


def _acted_items_path():
    from runtime_context import current_runtime_context
    context = current_runtime_context()
    if context is None:
        return _ACTED_ITEMS_PATH
    from note_transactions import session_state_path
    directory = session_state_path(context) / 'cache'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Acted-item cache must not be a symlink')
    return str(directory / 'deep-acted-items.json')


def _load_acted_items() -> set[str]:
    """Load recently acted-on item texts (within TTL)."""
    if not os.path.isfile(_acted_items_path()):
        return set()
    try:
        from runtime_context import current_runtime_context
        if current_runtime_context() is not None:
            from operation_state import _read_private
            return set(json.loads(_read_private(Path(_acted_items_path()))))
        import time
        if time.time() - os.path.getmtime(_acted_items_path()) > _ACTED_TTL_SECONDS:
            os.remove(_acted_items_path())
            return set()
        with open(_acted_items_path(), "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_acted_items(items: set[str]) -> None:
    """Persist acted-on item texts (append to existing). Best-effort."""
    existing = _load_acted_items()
    combined = existing | items
    try:
        os.makedirs(os.path.dirname(_acted_items_path()), exist_ok=True)
        from runtime_context import current_runtime_context
        if current_runtime_context() is not None:
            from operation_state import _write_private
            _write_private(Path(_acted_items_path()), json.dumps(sorted(combined)).encode(), current_runtime_context())
        else:
            with open(_acted_items_path(), "w", encoding="utf-8") as f:
                json.dump(sorted(combined), f)
    except OSError as exc:
        print(f"[obsidian-brain] warning: could not save acted items: {exc}", file=sys.stderr)


def run_batch_edit(*, expected_revisions=None, operation_id=None) -> int | None:
    """Batch edit vault files (checkoffs, link additions).

    Reads JSON array of ``[filepath, old_text, new_text]`` triples from stdin.
    Prints ``Applied N/M edits``.
    Records acted-on items so they aren't re-recommended on next run.
    """
    import uuid
    from pathlib import Path
    from note_transactions import NoteMutation, apply_mutations, context_for_vault, record_read

    from obsidian_utils import load_config

    from runtime_context import current_runtime_context
    native_context = current_runtime_context()
    c = load_config()
    vault_root = str(native_context.vault_path.resolve()) if native_context else os.path.realpath(c["vault_path"])
    context = native_context or context_for_vault(vault_root)

    edits = json.loads(_read_stdin_capped())
    if not isinstance(edits, list) or any(
        not isinstance(edit, list) or len(edit) != 3 or
        any(not isinstance(value, str) for value in edit) for edit in edits
    ):
        raise ValueError('Batch edits must be path, old text, new text triples')
    revisions = {}
    if native_context is not None:
        from operation_state import read_artifact
        if not operation_id or not isinstance(expected_revisions, dict):
            raise ValueError('Native edits require prepared revisions and operation identity')
        manifest = json.loads(read_artifact(context, operation_id, 'source-manifest.json'))
        identity = {'host': context.host, 'session_key': context.session_key,
                    'vault': str(context.vault_path),
                    'project': str(context.canonical_project_root), 'operation_id': operation_id}
        if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in identity.items()):
            raise ValueError('Source manifest belongs to another native operation')
        sources = manifest.get('sources')
        if not isinstance(sources, dict):
            raise ValueError('Invalid prepared source revisions')
        selected = set()
        for filepath, _, _ in edits:
            path = Path(filepath)
            real = path.resolve()
            if not path.is_absolute() or str(path) != str(real) or Path(vault_root) not in real.parents:
                raise ValueError('Edit path must be canonical and contained in selected vault')
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError('Edit paths cannot contain symbolic links')
            selected.add(str(real))
        if set(expected_revisions) != selected:
            raise ValueError('Prepared revisions must match selected edit paths')
        for path, revision in expected_revisions.items():
            if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision) or sources.get(path) != revision:
                raise ValueError('Edit revision was not prepared for this operation')
        revisions.update(expected_revisions)
    success = 0
    acted_texts: set[str] = set()
    skipped_checkoffs: list[str] = []
    skipped_other: list[str] = []
    for filepath, old_text, new_text in edits:
        try:
            real_path = os.path.realpath(filepath)
            if not real_path.startswith(vault_root + os.sep) and real_path != vault_root:
                print(f"[obsidian-brain] path containment violation: {filepath}", file=sys.stderr)
                continue

            if native_context is not None:
                import stat
                descriptor = os.open(real_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(descriptor, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('Edit source must be a regular note')
                    raw = stream.read(1_000_001)
                    after = Path(real_path).stat()
                    if len(raw) > 1_000_000 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError('Edit source changed while reading or exceeds size limit')
                content = raw.decode('utf-8')
            else:
                with open(real_path, "r", encoding="utf-8", newline="") as f:
                    content = f.read()
            observed_revision = record_read(context, Path(real_path), content)
            revision = revisions.get(real_path, observed_revision)
            if observed_revision != revision:
                print(f"[obsidian-brain] edit failed {filepath}: source revision changed", file=sys.stderr)
                continue

            new_content = None
            if _is_checkbox_flip(old_text, new_text):
                # CHECKBOX FLIP: line-anchored replace. Find the FIRST line whose
                # full content (sans trailing newline) exactly equals old_text AND
                # is an unchecked checkbox, then replace ONLY that line. This kills
                # mode 2 (quoted-prose / substring) corruption — a prose line that
                # merely *contains* the item text is never touched.
                lines = content.splitlines(keepends=True)
                for idx, raw_line in enumerate(lines):
                    stripped = raw_line.rstrip("\r\n")
                    if stripped == old_text and _UNCHECKED_CHECKBOX_RE.match(stripped):
                        ending = raw_line[len(stripped):]  # preserve "\n" / "" / "\r\n"
                        lines[idx] = new_text + ending
                        new_content = "".join(lines)
                        break
                if new_content is None:
                    print(
                        f"[obsidian-brain] checkoff skipped (no matching checkbox line): {filepath}",
                        file=sys.stderr,
                    )
                    skipped_checkoffs.append(old_text)
            else:
                # NON-checkbox edit (e.g. link additions): legacy substring replace.
                if old_text in content:
                    new_content = content.replace(old_text, new_text, 1)
                else:
                    # Parity with the checkoff path: a non-checkbox edit whose
                    # old_text isn't present is a real miss — surface it instead
                    # of failing silently (only the Applied count would reflect it).
                    snippet = old_text if len(old_text) <= 80 else old_text[:77] + "..."
                    skipped_other.append(snippet)

            if new_content is not None:
                if native_context is not None and re.match(r'\ufeff?---[ \t]*\r?\n', new_content):
                    from note_transactions import _render
                    new_content = _render(new_content, {'metadata': json.dumps({
                        'author_host': context.host, 'operation_id': operation_id,
                    })})
                result = apply_mutations(context, [NoteMutation(
                    Path(real_path), revision, {"document": new_content},
                    uuid.uuid4().hex, file_mode=0o600,
                )])
                if result.status not in {"applied", "unchanged"}:
                    print(f"[obsidian-brain] edit failed {filepath}: {result.status}", file=sys.stderr)
                    continue
                if native_context is not None:
                    # Advance only to bytes this operation published, never to a
                    # newly observed user edit between two changes to one note.
                    revisions[real_path] = hashlib.sha256(new_content.encode('utf-8')).hexdigest()
                success += 1
                # Track the item text (strip checkbox prefix for matching)
                item_text = old_text.replace("- [ ] ", "").replace("- [x] ", "").strip()
                if item_text:
                    acted_texts.add(item_text)
        except (OSError, ValueError) as e:
            print(f"[obsidian-brain] edit failed {filepath}: {e}", file=sys.stderr)
    if acted_texts:
        _save_acted_items(acted_texts)
    print(f"Applied {success}/{len(edits)} edits")
    if skipped_checkoffs:
        print(f"Skipped {len(skipped_checkoffs)} checkoff(s) with no matching line:")
        for old_text in skipped_checkoffs:
            print(f"  - {old_text}")
    if skipped_other:
        print(f"Skipped {len(skipped_other)} non-checkbox edit(s) with no match:")
        for old_text in skipped_other:
            print(f"  - {old_text}")
    if native_context is not None:
        return 0 if success == len(edits) else 1
    return None


def run_build_checkoffs() -> None:
    """Re-resolve checkoff targets by TEXT before any write (#201 Guard B).

    Reads a JSON array from stdin; each element:
        {"file": "<basename or path>", "line": <int hint>, "text": "<canonical item text>"}

    For each item we IGNORE the classifier's line number entirely and
    text-anchor the target: among the file's unchecked ``- [ ] `` lines we act
    ONLY when EXACTLY ONE text-matches; if two or more match we REFUSE
    (ambiguous), and if none match we SKIP. The drift-prone ``line`` hint can
    never disambiguate among text-similar siblings, so it can never check off a
    different still-active item, and quoted-prose lines (no checkbox) are never
    targeted.

    Emits to stdout::

        {"edits": [[fullpath, old_text, new_text], ...],
         "skipped": [{"file":..., "line":..., "reason":...}, ...]}

    where each ``old_text`` is the EXACT current line content (no trailing
    newline) and ``new_text`` is that line with the first ``[ ]`` flipped to
    ``[x]`` — i.e. exactly what Guard A (run_batch_edit) line-matches. This
    function is PURE of writes: it only reads + emits, and is safe to unit-test.
    """
    from obsidian_utils import load_config

    c = load_config()
    vault_root = os.path.realpath(c["vault_path"])
    sessions_folder = c.get("sessions_folder", "claude-sessions")
    insights_folder = c.get("insights_folder", "claude-insights")

    raw = _read_stdin_capped()
    items = json.loads(raw) if raw.strip() else []

    edits: list[list[str]] = []
    skipped: list[dict] = []

    def _resolve_path(file_field: str) -> str | None:
        """Resolve a basename-or-path to a contained full path, else None."""
        if os.path.isabs(file_field) or os.sep in file_field:
            candidates = [file_field]
        else:
            candidates = [
                os.path.join(vault_root, sessions_folder, file_field),
                os.path.join(vault_root, insights_folder, file_field),
            ]
        for cand in candidates:
            real = os.path.realpath(cand)
            # containment check
            if not (real == vault_root or real.startswith(vault_root + os.sep)):
                continue
            if os.path.isfile(real):
                return real
        return None

    for item in items or []:
        file_field = item.get("file", "")
        line_hint = item.get("line")
        ref_text = item.get("text", "") or ""

        real = _resolve_path(file_field)
        if real is None:
            # Distinguish containment violation from plain not-found for the report.
            probe = os.path.realpath(
                file_field if (os.path.isabs(file_field) or os.sep in file_field)
                else os.path.join(vault_root, sessions_folder, file_field)
            )
            reason = ("containment" if not (
                probe == vault_root or probe.startswith(vault_root + os.sep)
            ) else "file not found")
            skipped.append({"file": file_field, "line": line_hint, "reason": reason})
            continue

        try:
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            print(f"[obsidian-brain] checkoff: cannot read {file_field}: {exc}", file=sys.stderr)
            skipped.append({"file": file_field, "line": line_hint, "reason": "file not found"})
            continue

        # Candidate lines = unchecked checkboxes.
        candidate_idxs = [
            i for i, ln in enumerate(lines)
            if _UNCHECKED_CHECKBOX_RE.match(ln.rstrip("\n"))
        ]

        # Resolve PURELY by text, NEVER by the classifier's line hint. The hint
        # `line` is drift-prone (the whole #201 premise) and anchor_text_matches
        # is loose (LCS>=25), so a drifted hint that happens to land on a
        # TEXT-SIMILAR SIBLING checkbox could otherwise disambiguate among
        # mutually-matching siblings and silently check off the WRONG still-active
        # item — re-introducing the exact bug on the hint path. So a hint may
        # NEVER override the ambiguity refusal: we compute ALL text-matching
        # candidates first and only act when exactly one matches. The `line`
        # value is retained in the skipped report for diagnostics, but is not
        # used to SELECT among multiple matches (it can't be trusted to).
        matches = [
            i for i in candidate_idxs
            if anchor_text_matches(lines[i].rstrip("\n"), ref_text)
        ]
        if len(matches) == 1:
            target_idx = matches[0]
        elif len(matches) > 1:
            skipped.append({
                "file": file_field, "line": line_hint,
                "reason": f"ambiguous text match ({len(matches)} candidates)",
            })
            continue
        else:  # len(matches) == 0
            skipped.append({
                "file": file_field, "line": line_hint,
                "reason": "no matching checkbox line",
            })
            continue

        old_text = lines[target_idx].rstrip("\n")
        new_text = old_text.replace("[ ]", "[x]", 1)
        edits.append([real, old_text, new_text])

    print(
        f"[obsidian-brain] checkoffs: resolved {len(edits)}, skipped {len(skipped)}",
        file=sys.stderr,
    )
    print(json.dumps({"edits": edits, "skipped": skipped}))
