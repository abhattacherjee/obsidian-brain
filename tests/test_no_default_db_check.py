"""Regression tests for scripts/ci-checks/no-default-db.py (GH #46 guard)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci-checks" / "no-default-db.py"


def _run(tests_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(tests_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_tests_dir_is_clean():
    """The current tests/ directory must pass the guard."""
    result = _run(REPO_ROOT / "tests")
    assert result.returncode == 0, (
        f"Real tests/ has violations:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_detects_bare_ensure_index(tmp_path):
    fixture = tmp_path / "test_bad.py"
    fixture.write_text(
        "import vault_index\n"
        "def test_bad(tmp_path):\n"
        "    vault_index.ensure_index(str(tmp_path), ['claude-sessions'])\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "ensure_index" in result.stdout
    assert "test_bad.py:3" in result.stdout


def test_detects_bare_rebuild_index(tmp_path):
    fixture = tmp_path / "test_bad.py"
    fixture.write_text(
        "import vault_index\n"
        "def test_bad(tmp_path):\n"
        "    vault_index.rebuild_index(str(tmp_path), ['claude-sessions'])\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "rebuild_index" in result.stdout


def test_detects_bare_deep_analysis_pipeline(tmp_path):
    fixture = tmp_path / "test_bad.py"
    fixture.write_text(
        "import open_item_dedup\n"
        "def test_bad(tmp_path):\n"
        "    open_item_dedup.deep_analysis_pipeline(\n"
        "        ['x'], '[]', 'out.json', str(tmp_path),\n"
        "        'claude-sessions', 'claude-insights')\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "deep_analysis_pipeline" in result.stdout


def test_accepts_db_path_kwarg(tmp_path):
    fixture = tmp_path / "test_good.py"
    fixture.write_text(
        "import vault_index\n"
        "def test_good(tmp_path):\n"
        "    vault_index.ensure_index(\n"
        "        str(tmp_path), ['claude-sessions'],\n"
        "        db_path=str(tmp_path / 'x.db'))\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_accepts_kwargs_expansion(tmp_path):
    """Callers forwarding **kwargs can't be statically verified; allow them."""
    fixture = tmp_path / "test_kwargs.py"
    fixture.write_text(
        "import vault_index\n"
        "def test_fwd(tmp_path, **extra):\n"
        "    vault_index.ensure_index(str(tmp_path), ['x'], **extra)\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0


def test_noqa_suppresses_violation(tmp_path):
    fixture = tmp_path / "test_mocked.py"
    fixture.write_text(
        "import vault_index\n"
        "def test_m(tmp_path):\n"
        "    vault_index.ensure_index(str(tmp_path), ['x'])  # noqa: no-default-db\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout


def test_ignores_non_target_functions(tmp_path):
    fixture = tmp_path / "test_other.py"
    fixture.write_text(
        "def some_helper(x): pass\n"
        "def test_x():\n"
        "    some_helper('x')\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0
