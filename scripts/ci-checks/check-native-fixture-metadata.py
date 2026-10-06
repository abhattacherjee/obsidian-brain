"""Validate fresh synthetic observation hashes; never certify native dispatch."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

CLIENTS = {'claude-code','codex-cli','codex-desktop'}
FIELDS = {'client','version','platform','status','fixture_sha256','manifest_sha256',
          'fixture_path','manifest_path','discovery_verified','dispatch_verified','reason'}


def _checked_hash(root, name, expected):
    if not isinstance(name,str) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Observation path must stay inside the artifact directory.')
    target=root/name
    if any(part.is_symlink() for part in (target,*target.parents)):
        raise ValueError('Observation artifacts must not contain symlinks.')
    target.resolve().relative_to(root.resolve())
    if not target.is_file() or target.stat().st_size > 1_000_000:
        raise ValueError('Observation artifact is absent or exceeds the input cap.')
    if not isinstance(expected,str) or re.fullmatch('[0-9a-f]{64}',expected) is None:
        raise ValueError('Observation hash is invalid.')
    if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
        raise ValueError('Observation artifact changed after capture.')


def validate(metadata,root,now=None):
    if set(metadata)!={'schema','tested_sha','recorded_at','synthetic_only','observations'}:
        raise ValueError('Unknown or missing observation metadata fields.')
    if metadata['schema']!=1 or metadata['synthetic_only'] is not True:
        raise ValueError('Only declared synthetic observations are accepted.')
    if not re.fullmatch('[0-9a-f]{40}',str(metadata['tested_sha'])):
        raise ValueError('Exact tested commit is required.')
    recorded=datetime.fromisoformat(metadata['recorded_at'].replace('Z','+00:00'))
    if recorded.tzinfo is None:
        raise ValueError('Observation timestamp needs a timezone.')
    age=((now or datetime.now(timezone.utc))-recorded).total_seconds()
    if age < -60 or age > 7*24*60*60:
        raise ValueError('Native observation is not fresh.')
    clients=[]
    for observation in metadata['observations']:
        if set(observation)!=FIELDS:
            raise ValueError('Unknown or missing native observation fields.')
        clients.append(observation['client'])
        if (observation['client'] not in CLIENTS or observation['platform'] not in {'macos','linux'}
                or observation['status']!='observed' or observation['discovery_verified'] is not True
                or not isinstance(observation['version'],str)
                or re.fullmatch('[A-Za-z0-9_.+-]{1,80}',observation['version']) is None):
            raise ValueError('Native discovery is unavailable or blocked, not observed.')
        if not isinstance(observation['dispatch_verified'],bool):
            raise ValueError('Dispatch observation must be explicit.')
        for kind in ['fixture','manifest']:
            _checked_hash(root,observation[kind+'_path'],observation[kind+'_sha256'])
    if len(clients)!=len(CLIENTS) or set(clients)!=CLIENTS:
        raise ValueError('All three native clients need one metadata observation.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',required=True,type=Path)
    args=parser.parse_args()
    if args.input.stat().st_size>256_000:
        parser.error('Metadata exceeds input cap.')
    try:validate(json.loads(args.input.read_text()),args.input.parent)
    except (ValueError,OSError,TypeError) as error:
        parser.error(str(error))
    print('Synthetic discovery metadata validated; native dispatch acceptance is separate.')


if __name__=='__main__':main()
