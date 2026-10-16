"""Required capabilities and native evidence cannot be waived for release."""
import argparse
import json
import hashlib
import re
import os
from pathlib import Path

HOSTS = {'claude','codex'}
CLIENTS = {'claude-code','codex-cli','codex-desktop'}


def _evidence_records(references, root, expected_sha):
    if not isinstance(references, list) or not references or root is None:
        raise ValueError('Hashed evidence artifacts are required.')
    records = []
    root = Path(root).resolve()
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise ValueError('Evidence needs an artifact path and SHA256.')
        name = reference['path']
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Evidence path must stay in its artifact directory.')
        target = root / name
        if any(part.is_symlink() for part in (target, *target.parents)):
            raise ValueError('Evidence must not use symlinks.')
        target.resolve().relative_to(root)
        if not target.is_file() or target.stat().st_size > 1_000_000:
            raise ValueError('Evidence is absent or exceeds the input cap.')
        expected = reference['sha256']
        if not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None:
            raise ValueError('Evidence hash is invalid.')
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Evidence bytes changed after verification.')
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get('tested_sha') != expected_sha or record.get('status') != 'passed':
            raise ValueError('Evidence must pass for the exact tested commit.')
        records.append(record)
    return records


def validate(matrix, ledger, expected_sha, evidence_root=None):
    errors=[]
    ids=[entry['id'] for entry in matrix['capabilities']]
    if len(ids) != len(set(ids)):
        errors.append('Capability IDs are not unique.')
    for entry in matrix['capabilities']:
        if set(entry['hosts']) != HOSTS:
            errors.append(entry['id'] + ': both invoking hosts must be declared')
        for host, state in entry['hosts'].items():
            status=state.get('status','')
            if status != 'supported' and status != 'n/a' and not status.startswith('unsupported:'):
                errors.append(entry['id'] + ': invalid capability status')
            if entry['required'] and (status != 'supported' or not state.get('version_range')
                                     or not state.get('fixture_provenance')):
                errors.append(entry['id'] + ': required acceptance is incomplete for ' + host)
    if ledger.get('tested_sha') != expected_sha:
        errors.append('Acceptance evidence does not match the tested commit.')
    if set(ledger.get('clients',{})) != CLIENTS:
        errors.append('Acceptance must declare all three native clients.')
    for client, evidence in ledger.get('clients',{}).items():
        if evidence.get('status') != 'passed' or not evidence.get('version') or not evidence.get('evidence'):
            errors.append(client + ': native client evidence is incomplete')
        if client == 'codex-desktop' and evidence.get('dispatch_verified') is not True:
            errors.append('Desktop native dispatch is not verified.')
        if evidence.get('status') == 'passed':
            try:
                records = _evidence_records(evidence.get('evidence'), evidence_root, expected_sha)
                if not any(record.get('kind') == 'native-client-dispatch'
                           and record.get('client') == client and record.get('version') == evidence.get('version')
                           and record.get('dispatch_verified') is True for record in records):
                    raise ValueError('Native client dispatch artifact is missing or mismatched.')
            except (ValueError, OSError, TypeError) as exc:
                errors.append(client + ': ' + str(exc))
    criteria=ledger.get('criteria',{})
    if set(criteria) != {str(i) for i in range(1,8)}:
        errors.append('All seven acceptance criteria must be present.')
    for criterion, evidence in criteria.items():
        if evidence.get('status') != 'passed' or not evidence.get('evidence'):
            errors.append('Criterion ' + criterion + ': acceptance incomplete')
        if evidence.get('status') == 'passed':
            try:
                records = _evidence_records(evidence.get('evidence'), evidence_root, expected_sha)
                if not any(record.get('kind') == 'acceptance-criterion' and record.get('criterion') == criterion
                           for record in records):
                    raise ValueError('Criterion artifact is missing or mismatched.')
            except (ValueError, OSError, TypeError) as exc:
                errors.append('Criterion ' + criterion + ': ' + str(exc))
    return errors



MAX_BUNDLE_BYTES = 48 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate evidence keys are ambiguous.')
        result[key] = value
    return result


def materialize_bundle(raw, matrix, expected_sha, destination):
    """Validate exact external evidence bytes in a new private CI directory."""
    if not isinstance(raw, str) or not raw or len(raw.encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise ValueError('External acceptance bundle is absent or exceeds 48 KiB.')
    if re.fullmatch('[0-9a-f]{40}', expected_sha) is None:
        raise ValueError('Expected feature commit SHA is invalid.')
    bundle = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(bundle, dict) or set(bundle) != {'ledger', 'artifacts'}:
        raise ValueError('Bundle must contain only ledger and artifacts.')
    ledger, artifacts = bundle['ledger'], bundle['artifacts']
    if not isinstance(ledger, dict) or not isinstance(artifacts, dict) or not artifacts:
        raise ValueError('Bundle ledger and artifact map are required.')
    references = []
    for group in ('clients', 'criteria'):
        entries = ledger.get(group)
        if not isinstance(entries, dict):
            raise ValueError('Bundle ledger is incomplete.')
        for entry in entries.values():
            if not isinstance(entry, dict) or not isinstance(entry.get('evidence'), list):
                raise ValueError('Bundle evidence references are invalid.')
            for reference in entry['evidence']:
                if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
                    raise ValueError('Bundle evidence references are invalid.')
                references.append(reference['path'])
    if any(not isinstance(name, str) for name in references) or set(references) != set(artifacts):
        raise ValueError('Artifact map must match the ledger references exactly.')
    for name, content in artifacts.items():
        # Direct JSON children avoid platform-dependent path aliases.
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.json', name) is None or name == 'acceptance-ledger.json':
            raise ValueError('Artifact path must be a safe JSON filename.')
        if not isinstance(content, str):
            raise ValueError('Artifact values must retain exact JSON text bytes.')
        record = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(record, dict):
            raise ValueError('Artifact must contain a JSON object.')
    destination = Path(destination)
    if any(part.is_symlink() for part in (destination, *destination.parents)):
        raise ValueError('Bundle destination must not use symlinks.')
    destination.mkdir(mode=0o700)  # Existing paths are never overwritten.
    for name, content in {**artifacts, 'acceptance-ledger.json': json.dumps(ledger)}.items():
        descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
    errors = validate(matrix, ledger, expected_sha, destination)
    if errors:
        raise ValueError('; '.join(errors))
    return destination


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--matrix',required=True,type=Path)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--ledger',type=Path)
    source.add_argument('--bundle-env',action='store_true')
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--sha',required=True)
    args=parser.parse_args()
    matrix=json.loads(args.matrix.read_text())
    if args.bundle_env:
        if args.output_dir is None:
            parser.error('--bundle-env requires --output-dir')
        try:
            materialize_bundle(os.environ.get('NATIVE_ACCEPTANCE_BUNDLE', ''), matrix, args.sha, args.output_dir)
        except (ValueError, OSError, TypeError) as exc:
            print(str(exc))
            return 1
        return 0
    errors=validate(matrix,json.loads(args.ledger.read_text()),args.sha,args.ledger.parent)
    for error in errors:print(error)
    return 1 if errors else 0


if __name__=='__main__':raise SystemExit(main())
