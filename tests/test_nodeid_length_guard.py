"""#412: collection fails on a node id long enough to break PYTEST_CURRENT_TEST."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CONFTEST = Path(__file__).with_name("conftest.py")

# Loads only the guard hook from tests/conftest.py; the path comes from an
# env var, never pasted into the source.
_SHIM = """\
import importlib.util, os
_spec = importlib.util.spec_from_file_location("_ob_guard", os.environ["OB_CONFTEST"])
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
pytest_collection_modifyitems = _mod.pytest_collection_modifyitems
"""

_TEST = """\
import pytest

@pytest.mark.parametrize("x", [1], ids=["{case_id}"])
def test_case(x):
    assert x == 1
"""


def _collect(tmp_path, case_id):
    (tmp_path / "conftest.py").write_text(_SHIM, encoding="utf-8")
    (tmp_path / "test_case.py").write_text(_TEST.format(case_id=case_id), encoding="utf-8")
    env = {"OB_CONFTEST": str(_CONFTEST), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_guard_fails_collection_on_long_parametrize_id(tmp_path):
    proc = _collect(tmp_path, "a" * 5000)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out  # pytest's usage-error exit code
    assert "over the 4096 limit" in out
    assert "test_case.py::test_case[aaaa" in out


def test_guard_passes_short_parametrize_id(tmp_path):
    proc = _collect(tmp_path, "short")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "test_case.py::test_case[short]" in out


def test_guard_boundary_is_inclusive(tmp_path):
    # "test_case.py::test_case[" + id + "]" is 25 characters plus the id.
    at_limit = _collect(tmp_path, "b" * (4096 - 25))
    assert at_limit.returncode == 0, at_limit.stdout + at_limit.stderr
    over = _collect(tmp_path, "b" * (4096 - 24))
    assert over.returncode == 4, over.stdout + over.stderr
    assert "node id is 4097 characters" in over.stdout + over.stderr
