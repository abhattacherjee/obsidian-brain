"""Partition full test files and require complete artifacts before coverage."""
import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def partitions(root, count):
    if count < 1:
        raise ValueError('Shard count must be positive')
    files = sorted(path for path in (root / 'tests').rglob('*.py')
                   if path.name.startswith('test_') or path.name.endswith('_test.py'))
    if len(files) < count:
        raise ValueError('Every shard must contain tests')
    groups, sizes = [[] for _ in range(count)], [0] * count
    # The measured Linux serial coverage run spends 24 minutes in this file.
    # Give it one shard; balance the remaining whole files across the others.
    security = root / 'tests/test_security.py'
    isolate_security = count > 1 and security in files
    available = range(1 if isolate_security else 0, count)
    for path in sorted(files, key=lambda p: (-p.stat().st_size, str(p))):
        index = (0 if isolate_security and path == security else
                 min(available, key=lambda i: (sizes[i], i)))
        name = path.relative_to(root).as_posix()
        if '\n' in name or '\r' in name or path.is_symlink():
            raise ValueError('Test paths must be regular files without newlines')
        groups[index].append(name)
        sizes[index] += path.stat().st_size
    if any(not group for group in groups):
        raise ValueError('Every shard must contain tests')
    return [sorted(group) for group in groups]


def write_manifest(root, count, index, destination):
    groups = partitions(root, count)
    if index < 0 or index >= count:
        raise ValueError('Shard index is out of range')
    payload = {'count': count, 'index': index, 'files': groups[index]}
    destination.write_text(json.dumps(payload, indent=2) + '\n')
    return groups[index]


def validate_artifacts(root, artifacts, count, coverage=False, prefix='shard'):
    expected = partitions(root, count)
    directories = {path.name for path in artifacts.iterdir() if path.is_dir()}
    if directories != {f'{prefix}-{index}' for index in range(count)}:
        raise ValueError('Missing or unexpected shard artifacts')
    data_files = []
    for index, files in enumerate(expected):
        folder = artifacts / f'{prefix}-{index}'
        manifest = folder / 'manifest.json'
        if folder.is_symlink() or not manifest.is_file() or manifest.is_symlink():
            raise ValueError('Missing or unsafe shard manifest')
        payload = json.loads(manifest.read_text())
        if payload != {'count': count, 'index': index, 'files': files}:
            raise ValueError('Shard file membership differs from the full partition')
        if coverage:
            data = folder / '.coverage'
            if not data.is_file() or data.is_symlink() or data.stat().st_size == 0:
                raise ValueError('Missing shard coverage data')
            data_files.append(data)
    return data_files


def validate_timing_artifact(folder):
    """Require the complete successful serial timing run before combining it."""
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Missing or unsafe timing artifact')
    if {path.name for path in folder.iterdir()} != {'.coverage', 'serial-timing.xml'}:
        raise ValueError('Missing or unexpected timing artifact files')
    data, report = folder / '.coverage', folder / 'serial-timing.xml'
    for path in (data, report):
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Missing or unsafe timing artifact file')
    if report.stat().st_size > 3_000_000:
        raise ValueError('Oversized timing report')
    try:
        xml = ET.fromstring(report.read_bytes())
    except ET.ParseError as exc:
        raise ValueError('Invalid timing report') from exc
    classes = {'tests.test_security.TestDecisionTimeIsBounded',
               'tests.test_security.TestPatternDecisionTimeIsBounded'}
    cases = list(xml.iter('testcase'))
    identities = {(case.get('classname'), case.get('name')) for case in cases}
    suites = list(xml.iter('testsuite'))
    # This reviewed count fails closed when timing tests are added or removed.
    if (len(suites) != 1 or suites[0].get('tests') != '256'
            or any(suites[0].get(field) != '0' for field in ('failures', 'errors', 'skipped'))
            or len(cases) != 256 or len(identities) != 256
            or {case.get('classname') for case in cases} != classes
            or any(not case.get('name') or case.get('file') != 'tests/test_security.py'
                   or len(case) for case in cases)
            or any(list(xml.iter(tag)) for tag in ('failure', 'error', 'skipped'))):
        raise ValueError('Timing report must contain 256 unique successful timing cases')
    return data


def combine_coverage(root, artifacts, count, prefix='shard', timing_artifact=None):
    import coverage
    data_files = validate_artifacts(root, artifacts, count, coverage=True, prefix=prefix)
    if timing_artifact is not None:
        data_files.append(validate_timing_artifact(timing_artifact))
    combined = coverage.Coverage(config_file=str(root / 'setup.cfg'),
                                 data_file=str(root / '.coverage-combined'))
    combined.combine(data_paths=[str(path) for path in data_files], strict=True, keep=True)
    combined.save()
    # source_pkgs also measures synthetic modules used by collection controls.
    # Real installed copies must have mapped back to hooks via coverage:paths.
    # Only the direct scratch helper path is allowed outside the checkout.
    checkout = root.resolve()
    fixtures = []
    for filename in sorted(combined.get_data().measured_files()):
        path = Path(filename).resolve()
        if checkout == path or checkout in path.parents:
            continue
        if not re.fullmatch(r'.*/pytest-coverage/popen-gw[0-9]+/test_[^/]+/skill_procedures\.py',
                            path.as_posix()):
            raise ValueError(f'Unexpected coverage source outside checkout: {filename}')
        fixtures.append(filename)
    for filename in fixtures:
        print(f'Excluded synthetic collection fixture: {filename}')
    print(f'Excluded {len(fixtures)} synthetic collection fixture module(s)')
    total = combined.report(include=[str(checkout) + '/*'])
    if total < 90:
        raise ValueError(f'Combined coverage {total:.2f}% is below 90%')
    print(f'Combined coverage: {total:.2f}% (required 90%)')
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['list', 'validate', 'coverage'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--index', type=int)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--prefix', default='shard')
    parser.add_argument('--timing-artifact', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'list':
            if args.index is None or args.manifest is None:
                parser.error('list requires --index and --manifest')
            for name in write_manifest(args.root, args.count, args.index, args.manifest):
                print(name)
        else:
            if args.artifacts is None:
                parser.error('validate and coverage require --artifacts')
            if args.command == 'coverage':
                combine_coverage(args.root, args.artifacts, args.count, prefix=args.prefix,
                                 timing_artifact=args.timing_artifact)
            else:
                validate_artifacts(args.root, args.artifacts, args.count, prefix=args.prefix)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Shard check failed: {exc}\n')


if __name__ == '__main__':
    main()
