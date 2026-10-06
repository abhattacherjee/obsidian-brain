"""Fixed authored skill operations. Native context is supplied by brain_cli."""
from __future__ import annotations
from contextlib import redirect_stderr, redirect_stdout
import json
import hashlib
import io
import os
import sys
from pathlib import Path
import uuid
from contextvars import ContextVar
_SKILL_NAME = ContextVar('authored_skill_name', default=None)

def context_skill_name():
    return _SKILL_NAME.get()

from runtime_context import using_runtime_context
SKILLS = ('obsidian-setup', 'vault-config', 'recall', 'vault-search', 'vault-ask', 'compress', 'decide', 'error-log', 'retro', 'standup', 'check-items', 'consolidate', 'emerge', 'link', 'vault-import', 'vault-doctor', 'vault-stats', 'vault-reindex', 'dev-test')

def _emit(value):
    print(json.dumps(value))

def _config(context, payload):
    from obsidian_utils import indexed_folders
    value = dict(context.config)
    value['vault_path'] = str(context.vault_path)
    value['index_path'] = str(context.index_path)
    value['config_path'] = str(context.config_path)
    value['state_path'] = str(context.state_path)
    value['folders'] = indexed_folders(value)
    if value.get('wiki_folder') not in value['folders']:
        value['wiki_folder'] = ''
    value['project'] = context.canonical_project_root.name
    _emit(value)

def _session(context, payload):
    from obsidian_utils import get_session_context, resolve_source_session_note
    value = get_session_context(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'))
    value['agent_provider'] = context.host
    value['agent_session_id'] = context.native_session_id
    value['resolved_source_session_note'] = resolve_source_session_note(value['session_note_name'], value['session_id'], str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'))
    _emit(value)

def _note_path(context, payload):
    from note_writer import _resolve_note_path
    path, error = _resolve_note_path(str(context.vault_path), payload['path'])
    if error:
        raise ValueError(error)
    return Path(path)

def _note_read(context, payload):
    import stat
    from note_transactions import record_read
    path = _note_path(context, payload)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('The source must be a regular note.')
        raw = stream.read(1000001)
        after = path.stat()
        if len(raw) > 1000000 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('The source changed while reading or exceeds the note limit.')
    content = raw.decode('utf-8')
    revision = record_read(context, path, content)
    directory = _operation_directory(context, payload)
    manifest_path = directory / 'source-manifest.json'
    manifest = _source_manifest(context, payload, required=False)
    prepared_revision = manifest['sources'].get(str(path))
    if prepared_revision is not None and prepared_revision != revision:
        raise ValueError('Prepared source changed; begin a new operation before analyzing it again.')
    manifest['sources'][str(path)] = revision
    __import__('operation_state').store_artifact(context, payload['operation_id'], 'source-manifest.json', json.dumps(manifest))
    _emit({'path': str(path), 'content': content, 'expected_revision': revision, 'operation_id': payload['operation_id']})

def _note_apply(context, payload):
    from note_writer import _validate_note_content
    from note_transactions import NoteMutation, apply_mutations
    path = _note_path(context, payload)
    if 'expected_revision' not in payload:
        raise ValueError('Read and bind the destination revision before preparing edits.')
    content = _authored_content(context, payload, payload['content'])
    error = _validate_note_content(content)
    if error:
        raise ValueError(error)
    if 'expected_revision' not in payload:
        raise ValueError('Read and bind the destination revision before preparing edits.')
    manifest = _source_manifest(context, payload)
    if manifest['sources'].get(str(path)) != payload['expected_revision']:
        raise ValueError('The revision was not prepared for this note and operation.')
    result = apply_mutations(context, [NoteMutation(path, payload['expected_revision'], {'document': content}, 'curated-' + payload['operation_id'] + '-' + hashlib.sha256(str(path).encode()).hexdigest()[:16], file_mode=384)])
    _emit({'status': result.status, 'path': str(path), 'warnings': list(result.warnings)})
    return 0 if result.status in {'applied', 'unchanged'} else 1

def _note_create(context, payload):
    from note_writer import run_write
    _operation_directory(context, payload)
    return run_write(str(context.vault_path), payload['folder'], payload['filename'], _authored_content(context, payload, payload['content']))

def _upgrade_batch(context, payload):
    from obsidian_utils import upgrade_batch
    _emit(upgrade_batch(payload['paths'], str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), payload.get('project', 'unknown')))

def _consolidate(context, payload):
    from consolidate_cli import run_consolidate
    return run_consolidate(full=payload.get('full', False))

def _theme_stats(context, payload):
    from consolidate_cli import run_stats
    return run_stats()

def _theme_split(context, payload):
    from consolidate_cli import run_split
    return run_split(payload['theme_id'])

def _theme_merge(context, payload):
    from consolidate_cli import run_merge
    return run_merge(payload['a'], payload['b'])

def _emerge_themes(context, payload):
    from emerge_cli import run_emerge_themes
    return run_emerge_themes(payload.get('days', 30), operation_id=payload['operation_id'])

def _emerge_build(context, payload):
    from emerge_cli import run_build_note
    return run_build_note(operation_id=payload['operation_id'], themes_path=str(_scratch_path(context, payload, payload['themes_path'])), analysis_path=str(_scratch_path(context, payload, payload['analysis_path'])))

def _deep_pipeline(context, payload):
    from deep_cli import run_pipeline
    return run_pipeline(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), context.config.get('insights_folder', 'claude-insights'), operation_id=payload['operation_id'])

def _deep_present(context, payload):
    from deep_cli import run_present
    return run_present(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), context.config.get('insights_folder', 'claude-insights'), operation_id=payload['operation_id'], pipeline_path=str(_scratch_path(context, payload, payload['pipeline_path'])), classifications_path=str(_scratch_path(context, payload, payload['classifications_path'])))

def _deep_checkoffs(context, payload):
    from deep_cli import run_build_checkoffs
    return run_build_checkoffs()

def _deep_edit(context, payload):
    from deep_cli import run_batch_edit
    if 'expected_revisions' in payload:
        raise ValueError('Source revisions must come from the protected pre-analysis manifest.')
    edits = payload.get('stdin', [])
    if isinstance(edits, str):
        edits = json.loads(edits)
    if not isinstance(edits, list) or any(not isinstance(edit, list) or len(edit) != 3 or any(not isinstance(value, str) for value in edit) for edit in edits):
        raise ValueError('Batch edits must contain file, original text, and replacement text.')
    manifest = _source_manifest(context, payload)
    approved = []
    revisions = {}
    for filename, original, replacement in edits:
        path = _note_path(context, {'path': filename})
        revision = manifest['sources'].get(str(path))
        if not revision:
            raise ValueError('Read and bind every batch destination before analysis.')
        revisions[str(path)] = revision
        approved.append([str(path), original, replacement])
    sys.stdin = io.StringIO(json.dumps(approved))
    return run_batch_edit(expected_revisions=revisions, operation_id=payload['operation_id'])
OPERATIONS = {name: {'config': _config} for name in SKILLS}
for name in ('compress', 'decide', 'error-log', 'retro'):
    OPERATIONS[name]['session'] = _session
for name in ('compress', 'decide', 'error-log', 'retro', 'standup', 'vault-import', 'vault-stats'):
    OPERATIONS[name]['note-create'] = _note_create
for name in ('compress', 'link', 'recall', 'standup'):
    OPERATIONS[name].update({'note-read': _note_read, 'note-apply': _note_apply})
OPERATIONS['recall']['upgrade-batch'] = _upgrade_batch
OPERATIONS['standup'].update({'upgrade-batch': _upgrade_batch, 'deep-pipeline': _deep_pipeline, 'deep-present': _deep_present, 'deep-checkoffs': _deep_checkoffs, 'deep-edit': _deep_edit})
OPERATIONS['consolidate'].update({'consolidate': _consolidate, 'theme-stats': _theme_stats, 'theme-split': _theme_split, 'theme-merge': _theme_merge})
OPERATIONS['emerge'].update({'themes': _emerge_themes, 'build-note': _emerge_build})

def run_operation(context, skill_name, operation, payload, stdout, stderr):
    """Run an installed operation, preserving its output and exit status."""
    procedure = OPERATIONS.get(skill_name, {}).get(operation)
    if procedure is None or not isinstance(payload, dict):
        stderr.write(json.dumps({'code': 'operation_invalid', 'message': 'Unknown skill operation or invalid payload.'}) + '\n')
        return 2
    if set(payload) & {'host', 'client', 'resource_root', 'native_session_id', 'session_id', 'vault_path', 'config_path', 'index_path', 'state_path'}:
        stderr.write(json.dumps({'code': 'input_invalid', 'message': 'Native context cannot be overridden by an operation.'}) + '\n')
        return 2
    allowed = INPUTS.get(skill_name, {}).get(operation, ())
    values = payload.get('inputs', {})
    if not isinstance(values, dict) or set(values) - set(allowed) or any((not isinstance(value, str) for value in values.values())):
        stderr.write(json.dumps({'code': 'input_invalid', 'message': 'Unexpected procedure inputs.'}) + '\n')
        return 2
    old_environment = {key: os.environ.get(key) for key in allowed}
    old_argv, old_stdin = (sys.argv, sys.stdin)
    argv = payload.get('argv', [])
    if not isinstance(argv, list) or any((not isinstance(value, str) for value in argv)):
        stderr.write(json.dumps({'code': 'input_invalid', 'message': 'argv must contain strings.'}) + '\n')
        return 2
    skill_token = _SKILL_NAME.set(skill_name)
    try:
        for key in allowed:
            os.environ.pop(key, None)
        for key, value in values.items():
            _scratch_path(context, payload, value)
        os.environ.update(values)
        sys.argv = [operation] + argv
        input_value = payload.get('stdin', '')
        sys.stdin = io.StringIO(input_value if isinstance(input_value, str) else json.dumps(input_value))
        with using_runtime_context(context), redirect_stdout(stdout), redirect_stderr(stderr):
            result = procedure(context, payload)
        return result if isinstance(result, int) else 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except (ValueError, KeyError, TypeError, OSError) as exc:
        stderr.write(json.dumps({'code': 'operation_failed', 'message': str(exc)}) + '\n')
        return 1
    finally:
        _SKILL_NAME.reset(skill_token)
        sys.argv, sys.stdin = (old_argv, old_stdin)
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def _check_items_stage_01(context, payload):
    import sys, os, glob, json, subprocess
    import glob, json, os, re, sys
    from open_item_dedup import find_duplicates, cross_project_dedup
    from check_items_cache import canonical_hash, load_cache, partition
    from obsidian_utils import get_workspace_roots
    scope_path = os.environ['SCOPE_PATH']
    raw_path = os.environ['RAW_PATH']
    scope = _load_json(context, payload, scope_path)
    raw_items = _load_json(context, payload, raw_path)
    by_project = {}
    for it in raw_items:
        by_project.setdefault(it.get('project', 'unknown'), []).append(it)
    coarse_by_proj = {}
    for proj, items in by_project.items():
        tuples = [(it['path'], it['line'], it['text']) for it in items]
        seen_grouped = set()
        groups = []
        for idx, (fpath, line_num, item_text) in enumerate(tuples):
            if idx in seen_grouped:
                continue
            others = [(f, l, t) for j, (f, l, t) in enumerate(tuples) if j != idx]
            dupes = find_duplicates(item_text, others)
            members = [{'file': os.path.basename(fpath), 'line': line_num, 'text': item_text, 'mtime': os.path.getmtime(fpath) if os.path.exists(fpath) else 0, 'source_revision': items[idx].get('source_revision')}]
            for df, dl, dt, dc in dupes:
                for j, (f2, l2, t2) in enumerate(tuples):
                    if os.path.abspath(f2) == os.path.abspath(df) and l2 == dl:
                        seen_grouped.add(j)
                members.append({'file': os.path.basename(df), 'line': dl, 'text': dt, 'confidence': dc, 'mtime': os.path.getmtime(df) if os.path.exists(df) else 0, 'source_revision': next((item.get('source_revision') for item in items if os.path.abspath(item['path']) == os.path.abspath(df)), None)})
            seen_grouped.add(idx)
            import uuid
            g = {'group_id': str(uuid.uuid4())[:8], 'project': proj, 'representative': item_text, 'members': members, 'canonical_hash': canonical_hash(item_text)}
            groups.append(g)
        coarse_by_proj[proj] = groups
    flat_groups = cross_project_dedup(coarse_by_proj) if scope['mode'] == 'vault' else [g for v in coarse_by_proj.values() for g in v]
    cache = load_cache()
    known, needs = ([], [])
    heads = {}
    for proj, groups in coarse_by_proj.items():
        repo_path = None
        for _root in get_workspace_roots():
            _candidate = os.path.join(_root, proj)
            if os.path.isdir(os.path.join(_candidate, '.git')):
                repo_path = _candidate
                break
        if not repo_path:
            print(f'[check-items] no repo found for {proj} in {get_workspace_roots()}; forcing reclassify', file=sys.stderr)
            for g in groups:
                g['_reason'] = 'head_unavailable'
            needs.extend(groups)
            continue
        head_proc = subprocess.run(['git', '-C', repo_path, 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
        if head_proc.returncode != 0 or not head_proc.stdout.strip():
            print(f'[check-items] no HEAD for {proj} ({repo_path}); forcing reclassify', file=sys.stderr)
            for g in groups:
                g['_reason'] = 'head_unavailable'
            needs.extend(groups)
            continue
        head = head_proc.stdout.strip()
        heads[proj] = head
        k, n = partition(groups, cache, project=proj, head_sha=head, force=scope['no_cache'])
        known.extend(k)
        needs.extend(n)
    out = os.path.join(os.path.dirname(scope_path), 'partition.json')
    _store_json(context, payload, out, {'flat_groups': flat_groups, 'known': known, 'needs': needs, 'heads': heads})
    print(out)

def _check_items_stage_02(context, payload):
    import sys, os, glob, json
    import glob, json, os, re, sys
    from open_item_dedup import merge_groups_semantically, get_last_semantic_merge_mode
    scope_path = os.environ['SCOPE_PATH']
    part_path = os.environ['PART_PATH']
    data = _load_json(context, payload, part_path)
    needs = data['needs']
    known = data['known']
    needs_by_proj = {}
    for g in needs:
        needs_by_proj.setdefault(g['project'], []).append(g)
    merged_needs_by_proj = merge_groups_semantically(needs_by_proj) if needs_by_proj else {}
    mode = get_last_semantic_merge_mode() if needs_by_proj else 'ok'
    if mode != 'ok':
        raise ValueError('Semantic merge unavailable; operation remains pending without publication.')
    merged_by_proj = {}
    for proj, groups in merged_needs_by_proj.items():
        merged_by_proj.setdefault(proj, []).extend(groups)
    for g in known:
        merged_by_proj.setdefault(g['project'], []).append(g)
    out = os.path.join(os.path.dirname(scope_path), 'merged.json')
    _store_json(context, payload, out, {'merged_by_proj': merged_by_proj, 'mode': mode})
    print(out)

def _check_items_stage_03(context, payload):
    import sys, os, glob, json, datetime
    import glob, json, os, re, sys
    from open_item_dedup import deep_analysis_pipeline
    scope_path = os.environ['SCOPE_PATH']
    merged_path = os.environ['MERGED_PATH']
    scope = _load_json(context, payload, scope_path)
    data = _load_json(context, payload, merged_path)
    _config_path = str(context.config_path)
    try:
        config = dict(context.config, vault_path=str(context.vault_path))
        vault_path = config.get('vault_path')
        if not vault_path:
            raise ValueError('vault_path missing from config')
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f'ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup', file=sys.stderr)
        sys.exit(1)
    sessions_folder = config.get('sessions_folder', 'claude-sessions')
    insights_folder = config.get('insights_folder', 'claude-insights')
    window_days = scope.get('window_days', 14)
    cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    sessions_dir = os.path.join(vault_path, sessions_folder)
    basenames = []
    if os.path.isdir(sessions_dir):
        for fname in sorted(os.listdir(sessions_dir), reverse=True):
            if not fname.endswith('.md'):
                continue
            if len(fname) < 10 or fname[4] != '-' or fname[7] != '-':
                continue
            date_prefix = fname[:10]
            if date_prefix < cutoff:
                break
            basenames.append(fname)
    projects = list(data['merged_by_proj'].keys()) if isinstance(data['merged_by_proj'], dict) else []
    output_path = os.path.join(os.path.dirname(scope_path), 'pipeline_evidence.json')
    status = deep_analysis_pipeline(basenames=basenames, projects_json=json.dumps(projects), output_path=output_path, vault_path=vault_path, sessions_folder=sessions_folder, insights_folder=insights_folder, db_path=str(context.index_path), operation_id=payload['operation_id'])
    if not status.startswith('OK'):
        raise ValueError('Evidence collection failed; operation remains pending: ' + status)
    try:
        pipeline_data = _load_json(context, payload, output_path)
        evidence = pipeline_data.get('evidence', {})
        evidence_gaps = pipeline_data.get('evidence_gaps', {})
    except (OSError, json.JSONDecodeError) as e:
        print(f'WARNING: could not read pipeline output: {e}', file=sys.stderr)
        evidence = {}
        evidence_gaps = {}
    out = os.path.join(os.path.dirname(scope_path), 'evidence.json')
    _store_json(context, payload, out, evidence)
    gaps_out = os.path.join(os.path.dirname(scope_path), 'gaps.json')
    _store_json(context, payload, gaps_out, evidence_gaps)
    print(out)
    print(gaps_out)

def _check_items_stage_04(context, payload):
    import sys, os, glob, json
    import glob, json, os, re, sys
    from open_item_dedup import classify_groups_with_agent, classify_groups_heuristic, get_last_classifier_mode
    from check_items_cli import note_evidence_only_for
    scope_path = os.environ['SCOPE_PATH']
    merged_path = os.environ['MERGED_PATH']
    evidence_path = os.environ['EVIDENCE_PATH']
    data = _load_json(context, payload, merged_path)
    evidence = _load_json(context, payload, evidence_path)
    all_merged = [g for v in data['merged_by_proj'].values() for g in v] if isinstance(data['merged_by_proj'], dict) else data['merged_by_proj']
    from check_items_cache import build_classifier_provenance, partition, load_cache
    scope = _load_json(context, payload, scope_path)
    partition_path = os.path.join(os.path.dirname(scope_path), 'partition.json')
    captured = _load_json(context, payload, partition_path)
    provenance = build_classifier_provenance(context, all_merged, evidence)
    known, to_classify = [], []
    cache = load_cache()
    for project, groups in data['merged_by_proj'].items():
        head = captured.get('heads', {}).get(project)
        if head:
            hits, misses = partition(groups, cache, project, head, force=scope.get('no_cache', False), provenance=provenance)
            known.extend(hits)
            to_classify.extend(misses)
        else:
            to_classify.extend(groups)
    primary = classify_groups_with_agent(to_classify, evidence)
    mode = get_last_classifier_mode()
    for c in primary:
        c.setdefault('classifier_source', 'agent')
    _done_ids = {c.get('group_id') for c in primary}
    _missing = [g for g in to_classify if g.get('group_id') not in _done_ids]
    if mode != 'ok' or _missing or len(primary) != len(to_classify) or _done_ids != {g.get('group_id') for g in to_classify}:
        raise ValueError('Classifier incomplete; operation remains pending without publication.')
    classifications = list(primary)
    for g in known:
        if g.get('_cached_classification'):
            classifications.append({'group_id': g.get('group_id'), 'classification': g['_cached_classification'], 'confidence': g.get('_cached_confidence', 'LOW'), 'canonical_text': g.get('representative', ''), 'evidence_citation': g.get('_cached_evidence_citation'), 'action_required': g.get('_cached_action_required'), 'project': g.get('project'), 'classifier_source': 'cache', 'note_evidence_only': note_evidence_only_for(evidence, g.get('project', ''))})
    out = os.path.join(os.path.dirname(scope_path), 'classifications.json')
    _store_json(context, payload, out, {'classifications': classifications, 'classifier_mode': mode})
    _store_json(context, payload, os.path.join(os.path.dirname(scope_path), 'classifier-provenance.json'), {'groups': all_merged, 'evidence': evidence, 'provenance': provenance})
    print(out)

def _check_items_stage_05(context, payload):
    import sys, os, glob, json
    import glob, json, os, re, sys
    from open_item_dedup import assign_tier, partition_for_review
    scope_path = os.environ['SCOPE_PATH']
    classifications_path = os.environ['CLASSIFICATIONS_PATH']
    scope = _load_json(context, payload, scope_path)
    data = _load_json(context, payload, classifications_path)
    for item in data['classifications']:
        item['tier'] = assign_tier(item.get('evidence_citation'), item.get('canonical_text'), item.get('classification'), item.get('classifier_source'), item.get('note_evidence_only', False))
    buckets = partition_for_review(data['classifications'], show_all=scope['show_all'])
    mode = data.get('classifier_mode', 'ok')
    if mode != 'ok':
        _deg = sum((1 for i in data['classifications'] if i.get('classifier_source') == 'heuristic'))
        print(f"\n!! CLASSIFIER DEGRADED (mode={mode}) — {_deg} of {len(data['classifications'])} verdict(s) come from the token-overlap heuristic, not evidence.", file=sys.stderr)
        print("   Heuristic citations read 'token X near completion phrase Y'. That is co-occurrence, NOT proof the item is done.", file=sys.stderr)
        print("   A citation reading 'DONE rejected — ... but <cue> governs it' is the #299 conditional guard: the co-occurrence was a pending or forward-looking reference, so the item stayed open.", file=sys.stderr)
        print('   They are capped at tier MED and are never preselected. Verify each one manually before accepting.', file=sys.stderr)
    print('\n=== Review ===', file=sys.stderr)
    for item in sorted(buckets['review'], key=lambda x: ('HIGH MED LOW'.split().index(x.get('tier', 'LOW')), x.get('classification'))):
        mark = '[x]' if item['classification'] == 'DONE' and item['tier'] == 'HIGH' else '[ ]'
        _marker = ' [heuristic]' if item.get('classifier_source') == 'heuristic' else ''
        print(f"  {mark} ({item['classification']}/{item['tier']}) {item['canonical_text']}{_marker}", file=sys.stderr)
        print(f"      evidence: {item.get('evidence_citation')}", file=sys.stderr)
        if item.get('action_required'):
            print(f"      action:   {item['action_required']}", file=sys.stderr)
    out = os.path.join(os.path.dirname(scope_path), 'buckets.json')
    _store_json(context, payload, out, buckets)
    print(out)

def _check_items_stage_06(context, payload):
    import sys, os, glob, json, re, tempfile
    import glob, json, os, re, sys
    from open_item_dedup import cascade_group_members, parse_cascade_skipped_total
    scope_path = os.environ['SCOPE_PATH']
    buckets_path = os.environ['BUCKETS_PATH']
    merged_path = os.environ['MERGED_PATH']
    skips_file = os.environ.get('SKIPS_FILE', '')
    _config_path = str(context.config_path)
    try:
        config = dict(context.config, vault_path=str(context.vault_path))
        vault_path = config.get('vault_path')
        if not vault_path:
            raise ValueError('vault_path missing from config')
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f'ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup', file=sys.stderr)
        sys.exit(1)
    sessions_folder = config.get('sessions_folder', 'claude-sessions')
    sessions_dir = os.path.join(vault_path, sessions_folder)

    def _skip_key(path, line):
        return (os.path.realpath(str(path)), int(line))
    buckets = _load_json(context, payload, buckets_path)
    scope = _load_json(context, payload, scope_path)
    try:
        merged_data = _load_json(context, payload, merged_path)
        groups_by_id = {}
        for _gs in merged_data.get('merged_by_proj', {}).values():
            for _g in _gs:
                groups_by_id[_g.get('group_id')] = _g
    except (OSError, json.JSONDecodeError) as exc:
        print(f'[check-items] FATAL: cannot load merged.json ({exc}) — skipping cascade to avoid corrupt cache', file=sys.stderr)
        sys.exit(1)
    source_skips = set()
    if skips_file and os.path.exists(skips_file):
        try:
            raw_skips = _load_json(context, payload, skips_file)
            for entry in raw_skips or []:
                if isinstance(entry, list) and len(entry) == 2:
                    source_skips.add(_skip_key(entry[0], entry[1]))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f'[check-items] WARNING: source_skips load failed ({exc}); no item will be stamped applied and NO sibling cascade runs this run -- the checkoffs from steps 1-4 are on disk, but the report will show them open. Re-run /check-items to cascade.', file=sys.stderr)
            source_skips = set()
    for b in buckets['review']:
        gid = b.get('group_id')
        merged_group = groups_by_id.get(gid) if gid else None
        if not merged_group:
            continue
        for m in merged_group.get('members', []) or []:
            basename = m.get('file', '')
            line_num = m.get('line')
            if not basename or line_num is None:
                continue
            full_path = os.path.join(sessions_dir, basename)
            try:
                key = _skip_key(full_path, line_num)
            except (TypeError, ValueError):
                continue
            if key in source_skips:
                b['applied'] = True
                break
    _stamped = sum((1 for b in buckets['review'] if b.get('applied')))
    if source_skips and (not _stamped):
        print(f'[check-items] WARNING: {len(source_skips)} primary flip(s) recorded but none matched a grouped item, so no sibling cascade runs and the report shows them open. Recorded paths must be under {sessions_dir}.', file=sys.stderr)

    def _atomic_write_json(path, data):
        _store_json(context, payload, path, data)
    _atomic_write_json(buckets_path, buckets)
    groups_to_cascade = []
    for b in buckets['review']:
        if b.get('classification') != 'DONE':
            continue
        if not b.get('applied'):
            continue
        gid = b.get('group_id')
        merged_group = groups_by_id.get(gid) if gid else None
        if not merged_group:
            continue
        raw_members = merged_group.get('members', []) or []
        if not raw_members:
            continue
        resolved_members = []
        for m in raw_members:
            basename = m.get('file', '')
            line_num = m.get('line')
            if not basename or line_num is None:
                continue
            full_path = os.path.realpath(os.path.join(sessions_dir, basename))
            resolved_members.append({'file': full_path, 'line': line_num, 'text': m.get('text', ''), 'source_revision': m.get('source_revision')})
        if resolved_members:
            groups_to_cascade.append({'members': resolved_members})
    summary = cascade_group_members(groups_to_cascade, source_skips)
    print(f'[cascade] {summary}')
    m = re.search('Cascaded (\\d+)', summary)
    cascade_total = int(m.group(1)) if m else 0
    print(f'cascaded_total={cascade_total}')
    skipped_lines = [ln for ln in summary.splitlines() if ln.startswith('Skipped ') or ln.startswith('WRITE FAILED')]
    cascade_skipped_total = parse_cascade_skipped_total(summary)
    for ln in skipped_lines:
        print(f'[cascade-skip] {ln}')
    print(f'cascade_skipped_total={cascade_skipped_total}')
    cascade_summary_path = os.path.join(os.path.dirname(scope_path), 'cascade_summary.json')
    _atomic_write_json(cascade_summary_path, {'cascaded': cascade_total, 'skipped': cascade_skipped_total})
    if 'WRITE FAILED' in summary:
        print('[cascade] FATAL: a verified checkoff flip failed to save to disk -- do not report this run as fully successful', file=sys.stderr)
        sys.exit(1)

def _check_items_stage_07(context, payload):
    import sys, os, glob, json, re, subprocess, datetime
    import glob, json, os, re, sys
    from open_item_dedup import merge_records_from_groups
    from check_items_report import write_check_items_dashboard
    scope_path = os.environ['SCOPE_PATH']
    raw_path = os.environ['RAW_PATH']
    part_path = os.environ['PART_PATH']
    merged_path = os.environ['MERGED_PATH']
    classifications_path = os.environ['CLASSIFICATIONS_PATH']
    buckets_path = os.environ['BUCKETS_PATH']
    gaps_path = os.environ['GAPS_PATH']
    _config_path = str(context.config_path)
    try:
        config = dict(context.config, vault_path=str(context.vault_path))
        vault_path = config.get('vault_path')
        if not vault_path:
            raise ValueError('vault_path missing from config')
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f'ERROR: obsidian-brain config not loadable ({exc}); run /obsidian-setup', file=sys.stderr)
        sys.exit(1)
    scope = _load_json(context, payload, scope_path)
    if scope['mode'] == 'vault':
        scope_name = 'vault'
    elif scope['mode'] == 'project' and scope['project']:
        scope_name = scope['project']
    else:
        scope_name = context.canonical_project_root.name or 'unknown'
    date_str = datetime.date.today().isoformat()
    window_days = scope.get('window_days', 14)
    dry_run = bool(scope.get('dry_run', False))
    raw_count = len(_load_json(context, payload, raw_path))
    group_count = len(_load_json(context, payload, part_path).get('flat_groups', []))
    merged_data = _load_json(context, payload, merged_path)
    semantic_merge_mode = merged_data.get('mode', 'ok')
    merges = merge_records_from_groups(merged_data.get('merged_by_proj', {}))
    classifier_mode = _load_json(context, payload, classifications_path).get('classifier_mode', 'ok')
    buckets = _load_json(context, payload, buckets_path)
    classifications = buckets.get('review', []) + buckets.get('dashboard_only', [])
    applied = sum((1 for c in classifications if c.get('applied')))
    try:
        evidence_gaps = _load_json(context, payload, gaps_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'WARNING: could not read {gaps_path}: {exc} -- evidence_gaps omitted from this report', file=sys.stderr)
        evidence_gaps = None
    cascade_summary_path = os.path.join(os.path.dirname(scope_path), 'cascade_summary.json')
    try:
        cascade_summary = _load_json(context, payload, cascade_summary_path)
        cascaded = cascade_summary.get('cascaded', 0)
        skipped = cascade_summary.get('skipped', 0)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'WARNING: could not read {cascade_summary_path}: {exc} -- cascaded/skipped default to 0, which is WRONG if Step 8 ran and died before writing this file (correct only if Step 8 was skipped entirely)', file=sys.stderr)
        cascaded, skipped = (0, 0)
    report_path = write_check_items_dashboard(vault_path=vault_path, scope_name=scope_name, date_str=date_str, window_days=window_days, raw_count=raw_count, group_count=group_count, classifications=classifications, applied=applied, cascaded=cascaded, merges=merges, semantic_merge_mode=semantic_merge_mode, classifier_mode=classifier_mode, dry_run=dry_run, skipped=skipped, evidence_gaps=evidence_gaps)
    print(report_path)

def _check_items_stage_08(context, payload):
    import sys, os, glob, json, time
    import glob, json, os, re, sys
    from check_items_cache import locked_cache, update_cache, canonical_hash, build_classifier_provenance
    scope_path = os.environ['SCOPE_PATH']
    classifications_path = os.environ['CLASSIFICATIONS_PATH']
    partition_path = os.environ['PARTITION_PATH']
    scope = _load_json(context, payload, scope_path)
    data = _load_json(context, payload, classifications_path)
    part = _load_json(context, payload, partition_path)
    prepared = _load_json(context, payload, os.path.join(os.path.dirname(scope_path), 'classifier-provenance.json'))
    all_groups = prepared['groups']
    planned = build_classifier_provenance(context, all_groups, prepared['evidence'])
    if planned != prepared['provenance']:
        raise ValueError('Classifier selection changed; cache remains pending.')
    provenance = build_classifier_provenance(context, all_groups, prepared['evidence'], classifications=data['classifications'])
    merged_path_for_step10 = os.path.join(os.path.dirname(scope_path), 'merged.json')
    try:
        merged_data = _load_json(context, payload, merged_path_for_step10)
        all_merged = merged_data
    except (OSError, json.JSONDecodeError) as exc:
        print(f'[check-items] FATAL: cannot load merged.json ({exc}) — skipping cache update to avoid corrupt cache', file=sys.stderr)
        sys.exit(1)
    groups_by_id = {}
    for _proj, _gs in all_merged.get('merged_by_proj', {}).items():
        for _g in _gs:
            groups_by_id[_g.get('group_id')] = _g
    fresh_classifications = []
    for c in data['classifications']:
        gid = c.get('group_id')
        group = groups_by_id.get(gid, {})
        fresh_classifications.append({'group_id': gid, 'canonical_hash': group.get('canonical_hash') or canonical_hash(c.get('canonical_text', '')), 'canonical_text': c.get('canonical_text', ''), 'members': group.get('members', []), 'classification': c.get('classification'), 'confidence': c.get('confidence'), 'evidence_citation': c.get('evidence_citation'), 'classifier_source': c.get('classifier_source', ''), 'ai_backend': c.get('ai_backend'), 'ai_model': c.get('ai_model'), 'ai_prompt_version': c.get('ai_prompt_version'), 'ai_prompt_sha256': c.get('ai_prompt_sha256'), 'classified_ts': int(time.time()), '_group_project': group.get('project', 'unknown')})
    groups_by_proj = {}
    for g in all_groups:
        groups_by_proj.setdefault(g.get('project', 'unknown'), []).append(g)
    fresh_by_proj = {}
    for fc in fresh_classifications:
        proj = fc.pop('_group_project', 'unknown')
        fresh_by_proj.setdefault(proj, []).append(fc)
    heads = part.get('heads', {})
    try:
        with locked_cache() as cache:
            for proj, proj_groups in groups_by_proj.items():
                head = heads.get(proj)
                if not head:
                    print(f'[check-items] no head captured at Step 3 for {proj}; skipping cache update', file=sys.stderr)
                    continue
                update_cache(cache=cache, project=proj, all_groups=proj_groups, fresh_classifications=fresh_by_proj.get(proj, []), head_sha=head, provenance=provenance)
    except Exception as exc:
        print(f'cache NOT updated: {exc}')
        sys.exit(1)
    print('cache updated')
OPERATIONS['check-items']['stage-01'] = _check_items_stage_01
OPERATIONS['check-items']['stage-02'] = _check_items_stage_02
OPERATIONS['check-items']['stage-03'] = _check_items_stage_03
OPERATIONS['check-items']['stage-04'] = _check_items_stage_04
OPERATIONS['check-items']['stage-05'] = _check_items_stage_05
OPERATIONS['check-items']['stage-06'] = _check_items_stage_06
OPERATIONS['check-items']['stage-07'] = _check_items_stage_07
OPERATIONS['check-items']['stage-08'] = _check_items_stage_08
INPUTS = {'check-items': {'stage-01': ['RAW_PATH', 'SCOPE_PATH'], 'stage-02': ['PART_PATH', 'SCOPE_PATH'], 'stage-03': ['MERGED_PATH', 'SCOPE_PATH'], 'stage-04': ['EVIDENCE_PATH', 'MERGED_PATH', 'SCOPE_PATH'], 'stage-05': ['CLASSIFICATIONS_PATH', 'SCOPE_PATH'], 'stage-06': ['BUCKETS_PATH', 'MERGED_PATH', 'SCOPE_PATH', 'SKIPS_FILE'], 'stage-07': ['BUCKETS_PATH', 'CLASSIFICATIONS_PATH', 'GAPS_PATH', 'MERGED_PATH', 'PART_PATH', 'RAW_PATH', 'SCOPE_PATH'], 'stage-08': ['CLASSIFICATIONS_PATH', 'PARTITION_PATH', 'SCOPE_PATH']}}

def _operation_directory(context, payload):
    import re
    from operation_state import operation_directory
    operation_id = payload.get('operation_id')
    if not isinstance(operation_id, str) or not re.fullmatch('[a-f0-9]{32}', operation_id):
        raise ValueError('A private prepared operation_id is required.')
    return operation_directory(context, operation_id)[1]

def _scratch_path(context, payload, value):
    if not isinstance(value, str) or not value:
        raise ValueError('Scratch paths must be explicit absolute paths.')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('Scratch paths must be absolute and cannot be symbolic links.')
    path.resolve().relative_to(_operation_directory(context, payload))
    return path

def _operation_prepare(context, payload):
    from operation_state import operation_directory
    operation_id, path = operation_directory(context)
    _emit({'operation_id': operation_id, 'operation_dir': str(path)})

def _semantic_merge(context, payload):
    from check_items_cli import run_semantic_merge
    path = _scratch_path(context, payload, payload['output_path'])
    value = payload.get('stdin', {})
    return run_semantic_merge(value if isinstance(value, str) else json.dumps(value), str(path))

def _classify(context, payload):
    from check_items_cli import run_classifier
    path = _scratch_path(context, payload, payload['output_path'])
    value = payload.get('stdin', {})
    return run_classifier(value if isinstance(value, str) else json.dumps(value), str(path))

def _cache_partition(context, payload):
    from check_items_cache import load_cache, partition
    known, needs = partition(payload['groups'], load_cache(), payload['project'], payload['head_sha'], force=payload.get('force', False), provenance=payload.get('provenance'))
    _emit({'known': known, 'needs': needs})

def _cache_update(context, payload):
    from check_items_cache import locked_cache, update_cache
    with locked_cache() as cache:
        update_cache(cache, payload['project'], payload['groups'], payload['classifications'], payload['head_sha'], provenance=payload.get('provenance'))
    _emit({'status': 'ok'})
for _operations in OPERATIONS.values():
    _operations['prepare'] = _operation_prepare
OPERATIONS['check-items'].update({'semantic-merge': _semantic_merge, 'classify': _classify, 'partition': _cache_partition, 'update-cache': _cache_update})

def _publish_config(context, value, expected_revision):
    import hashlib
    import tempfile
    from operation_state import _no_symlinks
    import stat
    path = Path(context.config_path)
    if path.suffix != '.json':
        raise ValueError('Configuration destination must be JSON.')
    if path.resolve().is_relative_to(context.vault_path.resolve()):
        raise ValueError('Configuration must remain outside the selected vault.')
    _no_symlinks(path)
    if path.exists():
        details = path.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError('Configuration must be a private regular file owned by the current user.')
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.config-')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        _no_symlinks(path)
        raw = path.read_bytes() if path.exists() else b''
        if hashlib.sha256(raw).hexdigest() != expected_revision:
            raise ValueError('Configuration changed during publication; reload before saving.')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _source_manifest(context, payload, required=True):
    path = _operation_directory(context, payload) / 'source-manifest.json'
    identity = {'host': context.host, 'session_key': context.session_key, 'vault': str(context.vault_path), 'project': str(context.canonical_project_root), 'operation_id': payload['operation_id']}
    if path.is_symlink():
        raise ValueError('Source manifests cannot be symbolic links.')
    if not path.exists():
        if required:
            raise ValueError('Read the source before preparing edits or requesting AI.')
        return dict(identity, sources={})
    from operation_state import read_artifact
    manifest = json.loads(read_artifact(context, payload['operation_id'], 'source-manifest.json'))
    if not isinstance(manifest, dict) or any((manifest.get(key) != value for key, value in identity.items())):
        raise ValueError('Source manifest belongs to another native operation.')
    if not isinstance(manifest.get('sources'), dict):
        raise ValueError('Invalid prepared source revisions.')
    return manifest

def _authored_content(context, payload, content):
    from frontmatter import split_frontmatter, split_lines_lf_crlf
    _operation_directory(context, payload)
    eol = '\r\n' if '\r\n' in content else '\n'
    opened, fields, closed, body, error = split_frontmatter(split_lines_lf_crlf(content))
    if error:
        raise ValueError(error)
    keys = ('author_host:', 'operation_id:')
    fields = [line for line in fields if not line.startswith(keys)]
    fields.extend(['author_host: ' + context.host + eol, 'operation_id: ' + payload['operation_id'] + eol])
    return opened + ''.join(fields) + closed + ''.join(body)

def _config_read(context, payload):
    import hashlib
    raw = context.config_path.read_bytes() if context.config_path.exists() else b''
    if len(raw) > 1000000:
        raise ValueError('Configuration exceeds the input limit.')
    _emit({'config': json.loads(raw) if raw else {}, 'expected_revision': hashlib.sha256(raw).hexdigest(), 'config_path': str(context.config_path)})

def _configure(context, payload):
    import hashlib
    from note_transactions import ownership_lock
    from obsidian_utils import indexed_folders
    settings = payload['settings']
    if not isinstance(settings, dict):
        raise ValueError('Settings must be a JSON object.')
    allowed = {'vault_path', 'sessions_folder', 'insights_folder', 'dashboards_folder', 'check_items_folder', 'wiki_folder', 'index_path', 'min_messages', 'min_turns', 'min_duration_minutes', 'summary_model', 'auto_log_enabled', 'log_raw_messages', 'snapshot_on_compact', 'snapshot_on_clear', 'summary_timeout', 'summary_batch_size', 'summary_pipeline', 'codex_ai_model', 'codex_summary_model', 'optional_deps_prompted', 'optional_deps_declined'}
    if set(settings) - allowed:
        raise ValueError('Unknown configuration setting.')
    for key in ('auto_log_enabled', 'log_raw_messages', 'snapshot_on_compact', 'snapshot_on_clear', 'optional_deps_prompted'):
        if key in settings and (not isinstance(settings[key], bool)):
            raise ValueError(key + ' must be true or false.')
    for key in ('min_messages', 'min_turns', 'min_duration_minutes', 'summary_timeout', 'summary_batch_size'):
        if key in settings and (not isinstance(settings[key], int) or isinstance(settings[key], bool) or settings[key] <= 0):
            raise ValueError(key + ' must be a positive integer.')
    if 'optional_deps_declined' in settings and (not isinstance(settings['optional_deps_declined'], list) or any(item not in {'numpy', 'scipy'} for item in settings['optional_deps_declined'])):
        raise ValueError('Declined dependencies must name numpy or scipy.')
    expected = payload['expected_revision']
    with ownership_lock(context):
        raw = context.config_path.read_bytes() if context.config_path.exists() else b''
        if len(raw) > 1000000 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Configuration changed after it was read; reload it before saving.')
        config = json.loads(raw) if raw else {}
        config.update(settings)
        if Path(config.get('vault_path', '')).resolve() != context.vault_path.resolve():
            raise ValueError('Select the new vault explicitly before updating its configuration.')
        indexed_folders(config, strict=True)
        context.config_path.parent.mkdir(mode=448, parents=True, exist_ok=True)
        if context.config_path.is_symlink():
            raise ValueError('Configuration cannot be a symbolic link.')
        _publish_config(context, config, expected)
    _emit({'status': 'ok', 'config_path': str(context.config_path)})

def _reindex(context, payload):
    import time
    from obsidian_utils import load_config, indexed_folders
    from vault_index import rebuild_index
    started = time.monotonic()
    config = load_config(fresh=True)
    if Path(config.get('vault_path', '')).resolve() != context.vault_path.resolve():
        raise ValueError('Configuration vault changed; select it again before rebuilding.')
    folders = indexed_folders(config, strict=True)
    result = rebuild_index(str(context.vault_path), folders, full=payload.get('full', False))
    result['folders'] = [{'name': name, 'exists': (context.vault_path / name).is_dir()} for name in folders]
    result['elapsed'] = round(time.monotonic() - started, 1)
    result['mode'] = 'preserve' if 'preserved' in result else 'full'
    _emit(result)

def _wiki(context, payload, command):
    import wiki
    sys.stdin = io.StringIO(json.dumps(payload.get('data', {})))
    return wiki.main([command])

def _wiki_lookup(context, payload):
    return _wiki(context, payload, 'lookup')

def _wiki_stale(context, payload):
    return _wiki(context, payload, 'stale')

def _wiki_memgrep(context, payload):
    return _wiki(context, payload, 'memgrep')

def _wiki_count(context, payload):
    return _wiki(context, payload, 'count')

def _wiki_rule(context, payload):
    return _wiki(context, payload, 'rule')

def _wiki_file(context, payload):
    return _wiki(context, payload, 'file')
for _name in ('obsidian-setup', 'vault-config'):
    OPERATIONS[_name].update({'config-read': _config_read, 'configure': _configure})
for _name in ('obsidian-setup', 'vault-reindex'):
    OPERATIONS[_name]['reindex'] = _reindex
OPERATIONS['vault-ask'].update({'wiki-lookup': _wiki_lookup, 'wiki-stale': _wiki_stale, 'wiki-memgrep': _wiki_memgrep, 'wiki-count': _wiki_count, 'wiki-rule': _wiki_rule, 'wiki-file': _wiki_file})

def _store_json(context, payload, path, value):
    from operation_state import store_artifact
    selected = _scratch_path(context, payload, str(path))
    if selected.parent != _operation_directory(context, payload):
        raise ValueError('Artifacts must be direct children of the prepared operation.')
    return store_artifact(context, payload['operation_id'], selected.name, json.dumps(value, indent=2))

def _artifact_store(context, payload):
    from operation_state import store_artifact
    allowed = {'standup': {'deep-classifications.json', 'edits.json', 'skips.json'}, 'emerge': {'emerge-analysis.md'}, 'check-items': {'skips.json'}, 'vault-ask': {'wiki-payload.json'}}
    if payload['name'] not in allowed.get(context_skill_name(), set()):
        raise ValueError('Artifact name is reserved or not an approved helper output.')
    content = payload['content']
    if not isinstance(content, str):
        content = json.dumps(content)
    path = store_artifact(context, payload['operation_id'], payload['name'], content)
    _emit({'path': str(path)})
for _operations in OPERATIONS.values():
    _operations['artifact-store'] = _artifact_store

def _check_scope(context, payload):
    import difflib
    from check_items_args import parse_scope
    value = parse_scope(payload.get('argv', []))
    if value.unknown_tokens:
        nearby = sorted({project for project in value.known_projects if any((token.lower() in project.lower() or project.lower() in token.lower() for token in value.unknown_tokens))})
        for token in value.unknown_tokens:
            for project in difflib.get_close_matches(token, sorted(value.known_projects), n=3, cutoff=0.6):
                if project not in nearby:
                    nearby.append(project)
        print('ERROR: unrecognised argument(s): ' + ', '.join((repr(token) for token in value.unknown_tokens)), file=sys.stderr)
        if nearby:
            print('Did you mean: ' + ', '.join(nearby[:5]), file=sys.stderr)
        print('Valid forms: <project> | all | Nd | --show-all | --dry-run | --no-cache', file=sys.stderr)
        return 2
    scope = {key: getattr(value, key) for key in ('mode', 'project', 'window_days', 'show_all', 'dry_run', 'no_cache')}
    path = _operation_directory(context, payload) / 'scope.json'
    _store_json(context, payload, path, scope)
    print(path)

def _load_json(context, payload, path):
    from operation_state import read_artifact
    selected = _scratch_path(context, payload, str(path))
    if selected.parent != _operation_directory(context, payload):
        raise ValueError('Inputs must belong to the prepared operation.')
    return json.loads(read_artifact(context, payload['operation_id'], selected.name, supplied_path=str(selected)))

def _check_collect(context, payload):
    from obsidian_utils import read_note_metadata_detailed, indexed_folders
    from open_item_dedup import collect_open_item_records
    scope = _load_json(context, payload, payload['scope_path'])
    config = dict(context.config)
    config['vault_path'] = str(context.vault_path)
    indexed_folders(config, strict=True)
    folder = context.config.get('sessions_folder', 'claude-sessions')
    if scope['mode'] == 'vault':
        projects = set()
        directory = context.vault_path / folder
        for path in sorted(directory.glob('*.md')):
            path.resolve().relative_to(context.vault_path.resolve())
            metadata, error = read_note_metadata_detailed(str(path))
            if error:
                print('WARNING: skipped ' + str(path) + ': ' + error, file=sys.stderr)
            elif metadata and metadata.get('project'):
                projects.add(metadata['project'])
    elif scope['mode'] == 'project':
        projects = {scope['project']}
    else:
        if not (context.worktree / '.git').exists() and context.canonical_project_root == context.worktree:
            print('ERROR: /check-items current-project mode requires running inside a git repo.\nUse /check-items all (vault-wide) or /check-items <project>.', file=sys.stderr)
            return 1
        projects = {context.canonical_project_root.name}
    records = []
    for project in sorted(projects):
        records.extend(collect_open_item_records(str(context.vault_path), folder, project, max_sessions=50))
    path = _operation_directory(context, payload) / 'raw_items.json'
    _store_json(context, payload, path, records)
    print(path)
OPERATIONS['check-items'].update({'scope': _check_scope, 'collect': _check_collect})


def _metadata(context, payload):
    from vault_scan import note_meta
    from frontmatter import split_frontmatter, split_lines_lf_crlf
    from obsidian_utils import parse_frontmatter_field
    for argument in payload['paths']:
        result = note_meta(context.vault_path.resolve(), str(context.vault_path), argument)
        if not result.get('error'):
            path = Path(result['path'])
            if not path.is_absolute():
                path = context.vault_path / path
            path.resolve().relative_to(context.vault_path.resolve())
            if path.is_symlink() or not path.is_file():
                raise ValueError('Metadata source must be a contained regular note.')
            text = path.read_bytes().decode('utf-8')
            _, fields, _, _, error = split_frontmatter(split_lines_lf_crlf(text))
            if error:
                result['error'] = error
            else:
                frontmatter = ''.join(fields)
                for key in ('agent_provider', 'agent_session_id', 'capture_revision',
                            'summary_revision', 'capture_state', 'capture_completeness'):
                    result[key] = parse_frontmatter_field(frontmatter, key)
                if result.get('agent_provider'):
                    result['summary_stale'] = (not result.get('summary_revision') or
                        result.get('capture_revision') != result.get('summary_revision'))
                else:
                    result['summary_stale'] = False
        _emit(result)


def _search(context, payload):
    from obsidian_utils import indexed_folders
    from vault_index import ensure_index, search_vault
    config = dict(context.config, vault_path=str(context.vault_path))
    database = ensure_index(str(context.vault_path), indexed_folders(config))
    results = search_vault(database, payload['query'], limit=max(payload.get('limit', 20), 60),
                           project=payload.get('project'), note_type=payload.get('type'),
                           caller=payload.get('caller'))
    selected = []
    wiki_count = 0
    for result in results:
        if result.get('type') == 'claude-wiki':
            if wiki_count >= 3:
                continue
            wiki_count += 1
        metadata = io.StringIO()
        with redirect_stdout(metadata):
            _metadata(context, {'paths': [result['path']]})
        result.update(json.loads(metadata.getvalue()))
        selected.append(result)
        if len(selected) >= payload.get('limit', 20):
            break
    _emit(selected)


def _grep(context, payload):
    from vault_scan import main
    from obsidian_utils import indexed_folders
    folders = indexed_folders(dict(context.config, vault_path=str(context.vault_path)))
    arguments = ['grep', str(context.vault_path), *folders,
                 '--pattern=' + payload['pattern']]
    if payload.get('ignore_case'):
        arguments.append('--ignore-case')
    if payload.get('frontmatter_only'):
        arguments.append('--frontmatter-only')
    return main(arguments)


for _name in ('vault-search', 'vault-ask', 'link', 'standup', 'compress', 'decide', 'error-log'):
    OPERATIONS[_name].update({'search': _search, 'grep': _grep, 'metadata': _metadata})


def _clock(context, payload):
    from datetime import datetime, timezone
    print(datetime.now(timezone.utc).isoformat(timespec='seconds'))


def _sync(context, payload):
    from obsidian_utils import indexed_folders
    from vault_index import ensure_index
    ensure_index(str(context.vault_path), indexed_folders(dict(context.config)))
    print('OK')


def _unsummarized(context, payload):
    from obsidian_utils import find_unsummarized_notes
    _emit(json.loads(find_unsummarized_notes(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), payload.get('project', context.canonical_project_root.name))))


def _brief(context, payload):
    from obsidian_utils import build_context_brief
    print(build_context_brief(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), context.config.get('insights_folder', 'claude-insights'), payload.get('project', context.canonical_project_root.name)))


def _themes(context, payload):
    from obsidian_utils import recurring_themes_section
    print(recurring_themes_section(str(context.index_path), payload.get('project')))


def _snapshots(context, payload):
    from obsidian_utils import fetch_snapshot_summaries
    _emit(fetch_snapshot_summaries(context.vault_path / context.config.get('sessions_folder', 'claude-sessions'), payload['source_session_id'], payload.get('date', ''), payload['project']))


def _evidence(context, payload):
    from obsidian_utils import gather_session_evidence
    _emit(gather_session_evidence(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), context.config.get('insights_folder', 'claude-insights'), context.native_session_id, context.canonical_project_root.name, also_session_ids=payload.get('also_session_ids', [])))


def _retro_gate(context, payload):
    from obsidian_utils import mark_retro_classification_pending
    path = _note_path(context, payload)
    print(mark_retro_classification_pending(context.native_session_id, str(path)))


def _retro_clear(context, payload):
    from obsidian_utils import clear_retro_classification_pending
    print('cleared' if clear_retro_classification_pending(context.native_session_id) else 'no-gate')


def _summary_apply(context, payload):
    from obsidian_utils import upgrade_note_with_summary
    path = _note_path(context, payload)
    expected = payload['expected_revision']
    if _source_manifest(context, payload)['sources'].get(str(path)) != expected:
        raise ValueError('Read the source before preparing its summary.')
    result = upgrade_note_with_summary(str(path), payload['summary'], str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), payload.get('project', context.canonical_project_root.name), source=context.host + ' native skill', expected_revision=expected)
    print(result)
    return 0 if result.startswith('Upgraded ') else 1


def _dependencies(context, payload):
    from obsidian_utils import check_optional_deps
    _emit(dict(check_optional_deps(), optional_deps_prompted=context.config.get('optional_deps_prompted', False), optional_deps_declined=context.config.get('optional_deps_declined', [])))


def _stats(context, payload):
    from obsidian_utils import indexed_folders
    from vault_index import ensure_index
    from vault_stats import compute_stats
    database = ensure_index(str(context.vault_path), indexed_folders(dict(context.config)))
    result = compute_stats(database, payload.get('project', context.canonical_project_root.name))
    print(result)


def _doctor(context, payload):
    import importlib.util
    arguments = payload.get('argv', [])
    permitted = {'--check', '--days', '--project', '--min-confidence', '--strict', '--reconstruct', '--apply', '--yes', '--json'}
    for argument in arguments:
        if argument.startswith('-') and argument not in permitted:
            raise ValueError('Unsupported doctor flag: ' + argument)
    source = context.resource_root / 'scripts' / 'vault_doctor.py'
    spec = importlib.util.spec_from_file_location('_native_skill_doctor', source)
    module = importlib.util.module_from_spec(spec)
    script_directory = str(source.parent)
    sys.path.insert(0, script_directory)
    try:
        spec.loader.exec_module(module)
        return module.main()
    finally:
        sys.path.remove(script_directory)


def _dev_install(context, payload):
    import subprocess
    operation = payload.get('mode', 'install')
    if operation not in {'install', 'restore', 'status'}:
        raise ValueError('Unsupported developer installation mode.')
    script = context.resource_root / 'scripts' / 'test-dev-skill.sh'
    return subprocess.run(['bash', str(script), operation, '--host', context.host, '--source', str(context.resource_root)], stdin=subprocess.DEVNULL, timeout=120).returncode


for _name in ('compress', 'decide', 'error-log', 'retro'):
    OPERATIONS[_name].update({'clock': _clock, 'sync': _sync})
OPERATIONS['recall'].update({'unsummarized': _unsummarized, 'brief': _brief, 'recurring-themes': _themes, 'summary-apply': _summary_apply})
OPERATIONS['standup'].update({'unsummarized': _unsummarized, 'summary-apply': _summary_apply})
OPERATIONS['retro'].update({'evidence': _evidence, 'classification-pending': _retro_gate, 'classification-complete': _retro_clear})
for _name in ('vault-search', 'vault-ask'):
    OPERATIONS[_name]['snapshots'] = _snapshots
OPERATIONS['obsidian-setup']['dependencies'] = _dependencies
OPERATIONS['vault-stats']['stats'] = _stats
OPERATIONS['vault-doctor']['doctor'] = _doctor
OPERATIONS['dev-test']['dev-install'] = _dev_install


def _match_candidates(context, payload):
    import sys, os, json
    import glob, json, os, re, sys
    try:
        from vault_index import ensure_index, search_vault, compute_query_vector
        from obsidian_utils import load_config, indexed_folders
        from compress_guard import is_high_confidence_match, summarize_match_evidence, topic_snippet
        c = load_config()
        vp = c['vault_path']
        folders = indexed_folders(c)
        db = ensure_index(vp, folders)
        query_vec = compute_query_vector(db, payload['query'])
        results = search_vault(db, payload['query'], note_type='claude-insight', limit=3, include_vectors=True)
        results += search_vault(db, payload['query'], note_type='claude-decision', limit=3, include_vectors=True)
        results += search_vault(db, payload['query'], note_type='claude-session', limit=3, include_vectors=True)
        results.sort(key=lambda r: r['rank'])
        if is_high_confidence_match(results, query_vec=query_vec or None):
            top = results[0]
            ev = summarize_match_evidence(results, query_vec=query_vec or None)
            snippet = ''
            try:
                with open(top['path'], 'r', encoding='utf-8') as fh:
                    snippet = topic_snippet(fh.read(1000000))
            except (OSError, UnicodeDecodeError):
                pass
            print(json.dumps({'match': True, 'path': top['path'], 'title': top['title'], 'date': top['date'], 'tags': top['tags'], 'rank': top['rank'], 'rank_note': ev['rank_note'], 'runner_up_rank': ev['runner_up_rank'], 'shared_terms': ev['shared_terms'], 'snippet': snippet}))
        else:
            print(json.dumps({'match': False}))
    except ImportError as e:
        print(f'Warning: plugin hooks are out of date or missing ({e}) — run /dev-test install', file=sys.stderr)
        print(json.dumps({'match': False}))
    except Exception as e:
        print(f'Warning: could not search vault index: {e}', file=sys.stderr)
        print(json.dumps({'match': False}))
OPERATIONS['compress']['match-candidates'] = _match_candidates

OPERATIONS['obsidian-setup'].update({'note-create': _note_create, 'note-read': _note_read, 'note-apply': _note_apply})


def _import_read(context, payload):
    import hashlib
    import time
    from dataclasses import asdict, replace
    from transcripts import SourceCursor, read_records
    from session_lookup import find_existing_session
    source_host = payload['source_host']
    source_id = payload['source_session_id']
    source_path = Path(payload['source_path'])
    if source_host not in {'claude', 'codex'} or not isinstance(source_id, str) or not source_id.strip():
        raise ValueError('Historical import requires an explicit source host and full native ID.')
    if not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file():
        raise ValueError('Historical source must be an explicitly selected regular transcript.')
    origin = replace(context, host=source_host, native_session_id=source_id, transcript_path=source_path)
    deadline = time.monotonic() + 10
    existing = find_existing_session(origin, deadline)
    if existing:
        _emit({'status': 'skipped', 'existing_path': str(existing), 'source_host': source_host, 'source_session_id': source_id})
        return 0
    with source_path.open('rb') as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Historical source exceeds the bounded import size; operation remains pending.')
    revision = hashlib.sha256(raw).hexdigest()
    cursor = SourceCursor(historical=True)
    records = []
    metadata = {}
    while time.monotonic() < deadline:
        batch = read_records(origin, cursor, deadline)
        if batch.status not in {'ok', 'empty'} or batch.loss_of_input:
            raise ValueError('Historical source is incomplete; operation remains pending: ' + ', '.join(batch.warnings))
        records.extend(asdict(record) for record in batch.records)
        metadata.update(batch.metadata)
        if batch.source_complete:
            break
        if batch.consumed_offset <= cursor.offset:
            raise ValueError('Historical import made no bounded progress; operation remains pending.')
        cursor = SourceCursor(batch.source_generation, batch.consumed_offset, batch.source_identity, batch.anchor_digest, batch.parser_state, True, batch.source_size)
    else:
        raise ValueError('Historical import deadline reached; operation remains pending.')
    with source_path.open('rb') as stream:
        after = stream.read(16 * 1024 * 1024 + 1)
    if len(after) > 16 * 1024 * 1024 or hashlib.sha256(after).hexdigest() != revision:
        raise ValueError('Historical source changed during normalization; operation remains pending.')
    normalized = {'source_host': source_host, 'source_session_id': source_id, 'source_path': str(source_path), 'source_revision': revision, 'metadata': metadata, 'records': records}
    from operation_state import store_artifact
    path = store_artifact(context, payload['operation_id'], 'import-source.json', json.dumps(normalized))
    _emit(dict(normalized, path=str(path), status='ready'))


def _import_create(context, payload):
    import time
    import hashlib
    from dataclasses import replace
    from operation_state import read_artifact
    from session_lookup import find_existing_session
    from frontmatter import split_frontmatter, split_lines_lf_crlf
    source = json.loads(read_artifact(context, payload['operation_id'], 'import-source.json'))
    origin = replace(context, host=source['source_host'], native_session_id=source['source_session_id'], transcript_path=Path(source['source_path']))
    existing = find_existing_session(origin, time.monotonic() + 3)
    if existing:
        _emit({'status': 'skipped', 'existing_path': str(existing)})
        return 0
    source_path = Path(source['source_path'])
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError('Historical source is no longer regular; import remains pending.')
    with source_path.open('rb') as stream:
        current = stream.read(16 * 1024 * 1024 + 1)
    if len(current) > 16 * 1024 * 1024 or hashlib.sha256(current).hexdigest() != source['source_revision']:
        raise ValueError('Historical source changed during summary generation; import remains pending.')
    content = payload['content']
    opened, fields, closed, body, error = split_frontmatter(split_lines_lf_crlf(content))
    if error:
        raise ValueError(error)
    eol = '\r\n' if '\r\n' in content else '\n'
    fields = [line for line in fields if not line.startswith(('agent_provider:', 'agent_session_id:', 'session_id:'))]
    fields.extend(['agent_provider: ' + source['source_host'] + eol, 'agent_session_id: ' + source['source_session_id'] + eol, 'session_id: ' + source['source_session_id'] + eol])
    approved = dict(payload, folder=context.config.get('sessions_folder', 'claude-sessions'), content=opened + ''.join(fields) + closed + ''.join(body))
    return _note_create(context, approved)


OPERATIONS['vault-import'].update({'import-read': _import_read, 'note-create': _import_create})


def _import_list(context, payload):
    import time
    from datetime import datetime, timezone
    host = payload['source_host']
    if host not in {'claude', 'codex'}:
        raise ValueError('Select the historical source host explicitly.')
    from runtime_context import historical_source_roots
    roots = historical_source_roots(host, payload.get('source_root'))
    days = payload.get('days', 30)
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        raise ValueError('Import days must be a positive integer.')
    deadline = time.monotonic() + 10
    cutoff = time.time() - days * 86400
    project_filter = payload.get('project') or ''
    found = []
    visited = 0
    for directory in roots:
        if not directory.is_dir():
            continue
        for path in directory.rglob('*.jsonl'):
            visited += 1
            if visited > 2000 or time.monotonic() >= deadline:
                raise ValueError('Historical discovery exceeded its bound; select a narrower source root or date window.')
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime < cutoff:
                continue
            identity, project, timestamp = None, '', ''
            with path.open('rb') as stream:
                used = 0
                for _ in range(32):
                    row = stream.readline(65536 - used)
                    used += len(row)
                    if not row or used >= 65536:
                        break
                    try:
                        data = json.loads(row)
                    except (ValueError, UnicodeError):
                        break
                    if not isinstance(data, dict):
                        break
                    meta = data.get('payload', {}) if host == 'codex' and data.get('type') == 'session_meta' else data
                    if not isinstance(meta, dict):
                        continue
                    candidate = meta.get('id') if host == 'codex' and data.get('type') == 'session_meta' else meta.get('sessionId', meta.get('session_id')) if host == 'claude' else None
                    if isinstance(candidate, str) and candidate:
                        identity = candidate
                        project = meta.get('cwd', '')
                        timestamp = meta.get('timestamp', data.get('timestamp', ''))
                        break
            if identity and isinstance(project, str) and project_filter.lower() in project.lower():
                found.append({'source_host': host, 'session_id': identity, 'session_path': str(path), 'project': project, 'date': str(timestamp)[:10], 'git_branch': '', 'message_count': None})
    for value in sorted(found, key=lambda item: (item['date'], item['session_id']), reverse=True):
        _emit(value)


OPERATIONS['vault-import']['import-list'] = _import_list


def _cascade_collect(context, payload):
    from open_item_dedup import collect_open_item_records
    from operation_state import store_artifact
    records = collect_open_item_records(str(context.vault_path), context.config.get('sessions_folder', 'claude-sessions'), payload.get('project', context.canonical_project_root.name))
    path = store_artifact(context, payload['operation_id'], 'cascade-source.json', json.dumps(records))
    _emit({'path': str(path), 'count': len(records)})


def _cascade(context, payload):
    from operation_state import read_artifact
    from open_item_dedup import find_duplicates, cascade_group_members, parse_cascade_skipped_total
    source = json.loads(read_artifact(context, payload['operation_id'], 'cascade-source.json'))
    checked = payload['checked_texts']
    if not isinstance(checked, list) or any(not isinstance(item, str) for item in checked):
        raise ValueError('Cascade requires reviewed checked text strings.')
    instances = [(record['path'], record['line'], record['text']) for record in source]
    revisions = {record['path']: record['source_revision'] for record in source}
    selected = {}
    for text in checked:
        for path, line, item, confidence in find_duplicates(text, instances):
            if confidence == 'high':
                selected[(path, line)] = {'file': path, 'line': line, 'text': item, 'source_revision': revisions[path]}
    result = cascade_group_members([{'members': list(selected.values())}], vault_path=str(context.vault_path))
    print(result)
    return 1 if 'WRITE FAILED' in result.upper() or 'SOURCE REVISION CONFLICT' in result or parse_cascade_skipped_total(result) else 0


OPERATIONS['standup'].update({'cascade-collect': _cascade_collect, 'cascade': _cascade})


def _note_append(context, payload):
    from note_writer import run_append_update
    path = _note_path(context, payload)
    expected = payload['expected_revision']
    if _source_manifest(context, payload)['sources'].get(str(path)) != expected:
        raise ValueError('Read the existing note before preparing an update.')
    return run_append_update(str(context.vault_path), str(path), payload['update_text'], last_updated=payload.get('last_updated'), add_tags_csv=payload.get('add_tags_csv'), expected_revision=expected, author_host=context.host, operation_id=payload['operation_id'])


OPERATIONS['compress']['note-append'] = _note_append


def _note_status(context, payload):
    import hashlib
    from note_transactions import NoteMutation, apply_mutations
    path = _note_path(context, payload)
    expected = payload['expected_revision']
    if _source_manifest(context, payload)['sources'].get(str(path)) != expected:
        raise ValueError('Read and bind the source before updating its status.')
    if payload['status'] != 'summarized':
        raise ValueError('Unsupported authored status transition.')
    result = apply_mutations(context, [NoteMutation(path, expected, {'metadata': json.dumps({'status': 'summarized'})}, 'skill-status-' + payload['operation_id'] + '-' + hashlib.sha256(str(path).encode()).hexdigest()[:16])])
    _emit({'status': result.status, 'path': str(path)})
    return 0 if result.status in {'applied', 'unchanged'} else 1


OPERATIONS['standup']['status'] = _note_status


def _apply_reviewed(context, payload):
    """Apply selected checkoffs using the source bytes captured before AI."""
    import hashlib
    from note_transactions import NoteMutation, apply_mutations
    from open_item_dedup import anchor_text_matches
    identifier = payload['operation_id']
    directory = _operation_directory(context, payload)
    merged = _load_json(context, payload, directory / 'merged.json')
    buckets = _load_json(context, payload, directory / 'buckets.json')
    selected = payload['reviewed_group_ids']
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected) or len(set(selected)) != len(selected):
        raise ValueError('Reviewed groups must be distinct group IDs.')
    available = {item['group_id'] for item in buckets.get('review', [])}
    if not set(selected).issubset(available):
        raise ValueError('A reviewed group is absent from the current preview.')
    groups = {group['group_id']: group for values in merged['merged_by_proj'].values() for group in values}
    by_file = {}
    folder = context.vault_path / context.config.get('sessions_folder', 'claude-sessions')
    for group_id in selected:
        for member in groups[group_id]['members']:
            path = (folder / member['file']).resolve()
            path.relative_to(folder.resolve())
            path.relative_to(context.vault_path.resolve())
            by_file.setdefault(path, []).append(member)
    mutations = []
    flipped = 0
    for path, members in by_file.items():
        revisions = {member.get('source_revision') for member in members}
        if len(revisions) != 1 or None in revisions:
            raise ValueError('Missing or inconsistent pre-AI source revisions; review remains pending.')
        expected = next(iter(revisions))
        if path.is_symlink() or not path.is_file():
            raise ValueError('Reviewed source is no longer a regular note; review remains pending.')
        with path.open('rb') as stream:
            raw = stream.read(1000001)
        if len(raw) > 1000000 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('SOURCE REVISION CONFLICT; review remains pending without rebinding source.')
        lines = raw.decode('utf-8').splitlines(keepends=True)
        seen = set()
        for member in members:
            line = member['line']
            if not isinstance(line, int) or isinstance(line, bool) or line <= 0 or line > len(lines):
                raise ValueError('Reviewed line is invalid; review remains pending.')
            if line in seen:
                continue
            if not lines[line-1].lstrip().startswith('- [ ] ') or not isinstance(member.get('text'), str) or not member['text'].strip() or not anchor_text_matches(lines[line-1], member['text']):
                raise ValueError('Reviewed item no longer matches its source; review remains pending.')
            lines[line-1] = lines[line-1].replace('- [ ] ', '- [x] ', 1)
            seen.add(line)
            flipped += 1
        content = _authored_content(context, payload, ''.join(lines))
        event = 'reviewed-' + identifier + '-' + hashlib.sha256((str(path) + json.dumps(sorted(selected))).encode()).hexdigest()[:16]
        mutations.append(NoteMutation(path, expected, {'document': content}, event))
    result = apply_mutations(context, mutations)
    if result.status not in {'applied', 'unchanged'}:
        _emit({'status': 'pending', 'warnings': list(result.warnings)})
        return 1
    for item in buckets.get('review', []):
        if item['group_id'] in selected:
            item['applied'] = True
    _store_json(context, payload, directory / 'buckets.json', buckets)
    summary = {'cascaded': max(0, flipped-len(selected)), 'primary': len(selected), 'skipped': 0}
    _store_json(context, payload, directory / 'cascade_summary.json', summary)
    _emit(dict(summary, status='applied'))


OPERATIONS['check-items']['apply-reviewed'] = _apply_reviewed
