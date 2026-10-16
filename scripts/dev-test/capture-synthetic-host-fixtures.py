#!/usr/bin/env python3
"""Run isolated fixture capture; never certify installed native clients."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from importlib.metadata import version

TESTS = ('tests/test_host_behavior_conformance.py',
         'tests/test_native_lifecycle_integration.py',
         'tests/test_host_doctor_source_origin.py')


def capture(root, tested_sha):
    root = root.resolve()
    actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5)
    if actual.returncode != 0 or actual.stdout.strip() != tested_sha:
        raise ValueError('Requested commit differs from the current checkout')
    dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    if dirty.returncode != 0:
        raise ValueError('Cannot establish checkout state')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = os.pathsep.join(str(root / part) for part in ('tests', 'hooks', 'scripts'))
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'parity_collection_plugin',
                             *TESTS, '-q', '--parity-matrix', 'docs/parity/capabilities.json'],
                            cwd=root, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=180)
    paths = [*(root / 'tests/fixtures/hosts/golden').rglob('*'),
             *(root / 'hooks').rglob('*.py'),
             root / 'hooks/hooks.json', root / 'hooks/codex-hooks.json', root / '.codex/hooks.json',
             root / '.claude-plugin/plugin.json', root / '.codex-plugin/plugin.json',
             *(root / name for name in TESTS)]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(set(paths)) if path.is_file() and '__pycache__' not in path.parts}
    return {'schema':1, 'scope':'synthetic-host-conformance', 'tested_sha':tested_sha,
            'recorded_at':datetime.now(timezone.utc).isoformat(), 'status':'passed' if result.returncode == 0 else 'failed',
            'python_version':platform.python_version(), 'pytest_version':version('pytest'),
            'platform':platform.system().lower(), 'tests':list(TESTS), 'source_sha256':hashes,
            'native_client_versions':{'claude-code':None,'codex-cli':None,'codex-desktop':None},
            'native_dispatch_verified':False, 'installed_discovery':'not-run',
            'tracked_checkout_dirty':bool(dirty.stdout.strip()),
            'exit_code':result.returncode, 'harness_output':(result.stdout + result.stderr)[-1_000_000:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tested-sha', required=True)
    args = parser.parse_args()
    if len(args.tested_sha) != 40 or any(char not in '0123456789abcdef' for char in args.tested_sha):
        parser.error('Exact checkout commit is required')
    metadata = capture(Path(__file__).resolve().parents[2], args.tested_sha)
    print(json.dumps(metadata, indent=2))
    return metadata['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
