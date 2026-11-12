"""Config toggles reach session readers and skill redisplay immediately (#59)."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

import obsidian_utils
import retro_rollup


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SNIPPET_RE = re.compile(r"python3\s+-c\s+'(.*?)'", re.DOTALL)


def _snippet(skill, marker):
    text = (_REPO_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    matches = [body for body in _SNIPPET_RE.findall(text) if marker in body]
    if len(matches) != 1:
        pytest.fail(f"{skill}: expected one snippet containing {marker!r}, found {len(matches)}")
    return matches[0]


@pytest.fixture
def home_cfg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / "claude-insights").mkdir(parents=True)
    cfg_path = home / ".claude" / "obsidian-brain-config.json"
    cfg_path.write_text(
        json.dumps({
            "vault_path": str(vault),
            "insights_folder": "claude-insights",
            "lessons_enabled": False,
            "snapshot_on_clear": True,
        }),
        encoding="utf-8",
    )
    cfg_path.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(obsidian_utils, "_CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(
        obsidian_utils, "_get_session_id_fast",
        lambda *a, **k: "59595959-0000-0000-0000-000000000059",
    )
    return home, vault, cfg_path


def _run(code, *argv, home):
    result = subprocess.run(
        [sys.executable, "-c", code, *argv],
        env={**os.environ, "HOME": str(home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _ctx(vault, tmp_path):
    return retro_rollup.Ctx(retro_rollup._parser().parse_args([
        "report", "--vault", str(vault), "--state-dir", str(tmp_path / "state"),
    ]))


def test_lessons_enabled_toggle_reaches_rollup_and_retro_in_one_session(home_cfg, tmp_path):
    home, vault, _ = home_cfg
    assert _ctx(vault, tmp_path).lessons_enabled is False
    write = _snippet("vault-config", "config[sys.argv[1]] =")
    assert "OK" in _run(write, "lessons_enabled", "true", home=home)
    assert _ctx(vault, tmp_path).lessons_enabled is True
    read = _snippet("retro", 'print("off" if c.get("lessons_enabled") is False else "on")')
    assert _run(read, home=home).strip() == "on"


def test_snapshot_on_clear_toggle_is_seen_by_the_redisplay_read(home_cfg):
    home, _, _ = home_cfg
    assert obsidian_utils.load_config()["snapshot_on_clear"] is True
    write = _snippet("vault-config", "config[sys.argv[1]] =")
    assert "OK" in _run(write, "snapshot_on_clear", "false", home=home)
    assert obsidian_utils.load_config()["snapshot_on_clear"] is False


def test_step5_returns_to_step1_before_redisplay():
    text = (_REPO_ROOT / "skills" / "vault-config" / "SKILL.md").read_text(encoding="utf-8")
    assert "go back to Step 1 to re-read the config" in text
    assert "go back to Step 2 to redisplay" not in text


def test_folder_hint_no_longer_blames_the_config_cache():
    text = (_REPO_ROOT / "skills" / "vault-config" / "SKILL.md").read_text(encoding="utf-8")
    assert "read a cached copy of the config" not in text
    assert "/vault-reindex" in text
