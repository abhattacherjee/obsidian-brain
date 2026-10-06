import hashlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def checker():
    spec = importlib.util.spec_from_file_location('metadata_checker', ROOT / 'scripts/ci-checks/check-native-fixture-metadata.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def observation(tmp_path):
    fixture = tmp_path / 'synthetic.jsonl'
    manifest = tmp_path / 'manifest.json'
    fixture.write_bytes(b'{"synthetic":true}\n')
    manifest.write_bytes(b'{"hooks":[]}\n')
    metadata = {
        'schema': 1, 'tested_sha': 'a' * 40,
        'recorded_at': NOW.isoformat(), 'synthetic_only': True,
        'observations': [],
    }
    for client in ('claude-code', 'codex-cli', 'codex-desktop'):
        metadata['observations'].append({
            'client': client, 'version': '1.2.3', 'platform': 'macos',
            'status': 'observed', 'discovery_verified': True,
            'dispatch_verified': False, 'reason': 'synthetic discovery only',
            'fixture_path': fixture.name, 'manifest_path': manifest.name,
            'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
            'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
        })
    return metadata


def test_discovery_metadata_does_not_claim_dispatch(tmp_path):
    metadata = observation(tmp_path)
    checker().validate(metadata, tmp_path, NOW)
    assert all(row['dispatch_verified'] is False for row in metadata['observations'])


@pytest.mark.parametrize('status', ['blocked', 'unavailable'])
def test_unobserved_client_cannot_refresh_fixture(tmp_path, status):
    metadata = observation(tmp_path)
    metadata['observations'][2]['status'] = status
    with pytest.raises(ValueError, match='unavailable or blocked'):
        checker().validate(metadata, tmp_path, NOW)


@pytest.mark.parametrize('offset', [timedelta(days=-8), timedelta(minutes=2)])
def test_stale_or_future_observation_fails(tmp_path, offset):
    metadata = observation(tmp_path)
    metadata['recorded_at'] = (NOW + offset).isoformat()
    with pytest.raises(ValueError, match='not fresh'):
        checker().validate(metadata, tmp_path, NOW)


def test_changed_fixture_fails(tmp_path):
    metadata = observation(tmp_path)
    (tmp_path / 'synthetic.jsonl').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed after capture'):
        checker().validate(metadata, tmp_path, NOW)


def test_symlink_fixture_fails_even_with_matching_bytes(tmp_path):
    metadata = observation(tmp_path)
    fixture = tmp_path / 'synthetic.jsonl'
    original = tmp_path / 'original.jsonl'
    fixture.rename(original)
    fixture.symlink_to(original)
    with pytest.raises(ValueError, match='symlinks'):
        checker().validate(metadata, tmp_path, NOW)


def test_duplicate_client_cannot_hide_missing_observation(tmp_path):
    metadata = observation(tmp_path)
    metadata['observations'][2]['client'] = 'codex-cli'
    with pytest.raises(ValueError, match='All three'):
        checker().validate(metadata, tmp_path, NOW)
