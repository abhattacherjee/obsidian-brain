import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts/ci-checks'/name)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def test_unused_subprocess_helper_requires_exact_reviewed_inventory(tmp_path):
    import hashlib
    checker = module('host-test-contract.py')
    helpers = tmp_path / 'tests'
    helpers.mkdir()
    source = helpers / 'unused.py'
    source.write_text('import subprocess\ndef unused(command):\n'
                      '    return subprocess.run(command)\n')
    result = checker.inventory(tmp_path)
    assert checker.reviewed_processes(tmp_path, result, {'launchers': []})
    contract = {'source': 'tests/unused.py',
                'expression': 'subprocess.run(command)',
                'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'scope': 'support', 'reason': 'Disposable helper control'}
    matrix = {'launchers': [contract]}
    assert checker.reviewed_processes(tmp_path, result, matrix) == []
    source.write_text(source.read_text() + '# changed after review\n')
    assert checker.reviewed_processes(tmp_path, result, matrix)


def test_inventory_cannot_approve_a_launcher_without_actor_or_review_reason(tmp_path):
    import hashlib
    checker = module('host-test-contract.py')
    (tmp_path / 'tests').mkdir()
    source = tmp_path / 'tests/unused.py'
    source.write_text('import subprocess\ndef unused(command):\n'
                      '    return subprocess.run(command)\n')
    matrix = {'launchers': [{'source': 'tests/unused.py',
        'expression': 'subprocess.run(command)',
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'scope': 'support'}]}
    assert checker.reviewed_processes(tmp_path, checker.inventory(tmp_path), matrix)


@pytest.mark.parametrize('checked,evidence,passes',[
    ([], 'test evidence',False),
    ([0,1], 'test evidence',False),
    ([0], '',False),
    ([0], '<!-- placeholder -->',False),
    ([0], 'tests/test_host_behavior_conformance.py covers both hosts',True),
    ([2], 'Documentation typo; no runtime or descriptor changes',True),
])
def test_codex_impact_is_explicit(checked,evidence,passes):
    checker=module('check-codex-impact.py')
    body='\n'.join('- ['+('x' if index in checked else ' ')+'] '+label for index,label in enumerate(checker.LABELS))
    body+='\nImpact evidence: '+evidence
    if passes:checker.validate(body)
    else:
        with pytest.raises(ValueError):checker.validate(body)


def completed():
    matrix={'capabilities':[{'id':'writer.cas','required':True,'hosts':{h:{'status':'supported','version_range':'fixture-version','fixture_provenance':['fixed-golden']} for h in ['claude','codex']}}]}
    ledger={'tested_sha':'a'*40,'clients':{c:{'status':'passed','version':'fixture-version','evidence':['recorded native trace'],'dispatch_verified':True} for c in ['claude-code','codex-cli','codex-desktop']},'criteria':{str(i):{'status':'passed','evidence':['fixed result']} for i in range(1,8)}}
    return matrix,ledger


def test_required_behavior_cannot_waive_codex():
    matrix,ledger=completed();matrix['capabilities'][0]['hosts']['codex']['status']='unsupported:legacy-only'
    assert module('check-parity-acceptance.py').validate(matrix,ledger,'a'*40)


def test_bundled_binary_metadata_cannot_pass_desktop_dispatch():
    matrix,ledger=completed();ledger['clients']['codex-desktop']['dispatch_verified']=False
    assert 'Desktop native dispatch is not verified.' in module('check-parity-acceptance.py').validate(matrix,ledger,'a'*40)


def test_stale_tested_commit_cannot_pass_acceptance():
    matrix,ledger=completed()
    assert module('check-parity-acceptance.py').validate(matrix,ledger,'b'*40)


def hashed_completed(tmp_path):
    import hashlib
    import json
    matrix, ledger = completed()
    def store(name, record):
        record.update(tested_sha='a'*40, status='passed')
        raw = json.dumps(record).encode()
        (tmp_path / name).write_bytes(raw)
        return {'path':name, 'sha256':hashlib.sha256(raw).hexdigest()}
    for client, evidence in ledger['clients'].items():
        evidence['evidence'] = [store(client+'.json', {'kind':'native-client-dispatch',
            'client':client, 'version':'fixture-version', 'dispatch_verified':True})]
    for criterion, evidence in ledger['criteria'].items():
        evidence['evidence'] = [store('criterion-'+criterion+'.json',
            {'kind':'acceptance-criterion','criterion':criterion})]
    return matrix, ledger


def test_complete_hashed_evidence_is_checked_for_exact_commit(tmp_path):
    matrix, ledger = hashed_completed(tmp_path)
    assert module('check-parity-acceptance.py').validate(matrix, ledger, 'a'*40, tmp_path) == []


@pytest.mark.parametrize('mutation', ['missing', 'changed', 'foreign-commit', 'metadata-only', 'path-escape'])
def test_status_labels_cannot_replace_verified_native_artifacts(tmp_path, mutation):
    import hashlib
    import json
    matrix, ledger = hashed_completed(tmp_path)
    reference = ledger['clients']['codex-desktop']['evidence'][0]
    target = tmp_path / reference['path']
    if mutation == 'missing':
        target.unlink()
    elif mutation == 'changed':
        target.write_text('{}')
    elif mutation == 'path-escape':
        reference['path'] = '../outside.json'
    else:
        record = json.loads(target.read_text())
        if mutation == 'foreign-commit':
            record['tested_sha'] = 'b'*40
        else:
            record['kind'] = 'binary-metadata'
        raw = json.dumps(record).encode()
        target.write_bytes(raw)
        reference['sha256'] = hashlib.sha256(raw).hexdigest()
    assert module('check-parity-acceptance.py').validate(matrix, ledger, 'a'*40, tmp_path)


def external_bundle(tmp_path):
    import json
    matrix, ledger = hashed_completed(tmp_path)
    artifacts = {path.name:path.read_text() for path in tmp_path.glob('*.json')}
    return matrix, json.dumps({'ledger':ledger, 'artifacts':artifacts})


def test_external_bundle_passes_without_tracked_commit_self_reference(tmp_path):
    import json
    source = tmp_path / 'source'; source.mkdir()
    matrix, raw = external_bundle(source)
    target = tmp_path / 'private-ci'
    checker = module('check-parity-acceptance.py')
    assert checker.materialize_bundle(raw, matrix, 'a'*40, target) == target
    assert checker.validate(matrix, json.loads((target/'acceptance-ledger.json').read_text()),
                            'a'*40, target) == []
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in target.iterdir())


@pytest.mark.parametrize('mutation', ['empty','wrong-sha','tampered','path-escape','oversize',
                                     'missing-artifact','extra-artifact','duplicate-key','non-json','existing-target'])
def test_external_bundle_rejects_incomplete_or_ambiguous_evidence(tmp_path, mutation):
    import json
    source = tmp_path / 'source'; source.mkdir()
    matrix, raw = external_bundle(source)
    bundle = json.loads(raw); target = tmp_path/'private-ci'
    if mutation == 'empty': raw = ''
    elif mutation == 'wrong-sha': bundle['ledger']['tested_sha'] = 'b'*40
    elif mutation == 'tampered': bundle['artifacts']['codex-desktop.json'] = '{}'
    elif mutation == 'path-escape':
        old='codex-desktop.json'; new='../escaped.json'
        bundle['artifacts'][new] = bundle['artifacts'].pop(old)
        bundle['ledger']['clients']['codex-desktop']['evidence'][0]['path']=new
    elif mutation == 'oversize': raw = ' ' * (48*1024+1)
    elif mutation == 'missing-artifact': bundle['artifacts'].pop('codex-desktop.json')
    elif mutation == 'extra-artifact': bundle['artifacts']['unreviewed.json']='{}'
    elif mutation == 'duplicate-key': raw='{"ledger":{},"ledger":{},"artifacts":{}}'
    elif mutation == 'non-json': bundle['artifacts']['codex-desktop.json']='not JSON'
    elif mutation == 'existing-target':
        target.mkdir(); (target/'keep.json').write_text('human bytes')
    if mutation not in {'empty','oversize','duplicate-key'}: raw=json.dumps(bundle)
    with pytest.raises((ValueError, OSError)):
        module('check-parity-acceptance.py').materialize_bundle(raw,matrix,'a'*40,target)
    assert not (tmp_path/'escaped.json').exists()
    if mutation == 'existing-target': assert (target/'keep.json').read_text() == 'human bytes'


def test_acceptance_workflow_uses_external_feature_sha_evidence():
    source=(ROOT/'.github/workflows/host-conformance.yml').read_text()
    acceptance=source.split('  acceptance:\n',1)[1].split('  required-contracts:',1)[0]
    assert 'ref: ${{ github.event.pull_request.head.sha || github.sha }}' in acceptance
    assert "vars[format('P_{0}', github.event.pull_request.head.sha || github.sha)]" in acceptance
    assert '--bundle-env' in acceptance and '--ledger docs/parity/acceptance-ledger.json' not in acceptance
    assert 'native-acceptance-${{ github.event.pull_request.head.sha || github.sha }}' in acceptance


@pytest.mark.parametrize('declaration,expression', [
    ('from subprocess import check_output as co', 'co(command)'),
    ('import os', 'os.system(command)'),
    ('from os import system as sh', 'sh(command)'),
    ('import subprocess as sp', 'sp.run(command)'),
])
def test_unused_imported_process_alias_requires_review(tmp_path, declaration, expression):
    import hashlib
    checker = module('host-test-contract.py')
    (tmp_path / 'tests').mkdir()
    source = tmp_path / 'tests' / 'unused.py'
    source.write_text(declaration + '\ndef unused(command):\n    return ' + expression + '\n')
    result = checker.inventory(tmp_path)
    assert checker.reviewed_processes(tmp_path, result, {'launchers': []})
    contract = {'source': 'tests/unused.py', 'expression': expression,
                'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'scope': 'support', 'reason': 'Disposable unused helper control'}
    assert checker.reviewed_processes(tmp_path, result, {'launchers': [contract]}) == []


def test_inventory_cli_failure_and_reviewed_success(tmp_path, monkeypatch, capsys):
    import hashlib
    import json
    import sys
    checker = module('host-test-contract.py')
    (tmp_path / 'tests').mkdir()
    source = tmp_path / 'tests' / 'unused.py'
    source.write_text('from subprocess import check_output as co\ndef unused(command):\n    return co(command)\n')
    matrix = tmp_path / 'matrix.json'
    output = tmp_path / 'inventory.json'
    matrix.write_text(json.dumps({'launchers': []}))
    monkeypatch.setattr(sys, 'argv', ['host-test-contract.py', '--root', str(tmp_path),
                        '--output', str(output), '--parity-matrix', str(matrix)])
    assert checker.main() == 1
    assert json.loads(output.read_text())['errors']
    assert 'exact reviewed launcher contract' in capsys.readouterr().err
    matrix.write_text(json.dumps({'launchers': [{'source': 'tests/unused.py',
        'expression': 'co(command)', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'scope': 'support', 'reason': 'Disposable CLI exit control'}]}))
    assert checker.main() == 0
    assert json.loads(output.read_text())['errors'] == []


def test_unmatched_backticks_cannot_expose_comment_across_paragraphs():
    checker = module('check-codex-impact.py')
    body = ('Note the ` mark.\n\n<!--\n- [x] No Codex impact; explain why\n\n'
            'Impact evidence: hidden\n-->\n\nAnd the ` mark again.\n')
    with pytest.raises(ValueError, match='Choose exactly one'):
        checker.validate(body)
    checker.validate('- [x] No Codex impact; explain why\nImpact evidence: the literal `<!--` token.\n')


def test_inline_code_and_fence_boundaries_cannot_certify_hidden_impact():
    checker = module('check-codex-impact.py')
    bodies = [
        '`example\n- [x] No Codex impact; explain why\nImpact evidence: hidden\n`',
        'Unmatched ` mark.\n```text\nignored\n```\n<!--\n'
        '- [x] No Codex impact; explain why\nImpact evidence: hidden\n-->\nClosing ` mark.',
    ]
    for body in bodies:
        with pytest.raises(ValueError, match='Choose exactly one'):
            checker.validate(body)
