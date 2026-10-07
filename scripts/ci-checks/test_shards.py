"""Partition full test files and require complete artifacts before coverage."""
import argparse
import json
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


def combine_coverage(root, artifacts, count, prefix='shard'):
    import coverage
    data_files = validate_artifacts(root, artifacts, count, coverage=True, prefix=prefix)
    combined = coverage.Coverage(config_file=str(root / 'setup.cfg'),
                                 data_file=str(root / '.coverage-combined'))
    combined.combine(data_paths=[str(path) for path in data_files], strict=True, keep=True)
    combined.save()
    total = combined.report()
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
                combine_coverage(args.root, args.artifacts, args.count, prefix=args.prefix)
            else:
                validate_artifacts(args.root, args.artifacts, args.count, prefix=args.prefix)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Shard check failed: {exc}\n')


if __name__ == '__main__':
    main()
