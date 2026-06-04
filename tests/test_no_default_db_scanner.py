"""Self-test for the extended no-default-db raw-connect scanner (#192)."""
from __future__ import annotations

import importlib.util
import os

_SCANNER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "ci-checks", "no-default-db.py"
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("no_default_db", _SCANNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flags_raw_connect_in_non_allowed_function():
    mod = _load_scanner()
    src = "import sqlite3\n\ndef open_db(p):\n    return sqlite3.connect(p)\n"
    violations = mod.audit_raw_connect("hooks/other.py", src)
    assert violations, "expected a raw sqlite3.connect violation"


def test_allows_raw_connect_in_allowlisted_connect():
    mod = _load_scanner()
    src = "import sqlite3\n\ndef _connect(p):\n    return sqlite3.connect(p, timeout=5.0)\n"
    violations = mod.audit_raw_connect("hooks/vault_index.py", src)
    assert violations == [], "vault_index._connect must be allowlisted"


def test_noqa_suppresses_violation():
    mod = _load_scanner()
    src = "import sqlite3\n\ndef f(p):\n    return sqlite3.connect(p)  # noqa: vault-db-connect\n"
    violations = mod.audit_raw_connect("hooks/other.py", src)
    assert violations == [], "# noqa: vault-db-connect must suppress"


def test_extract_python_blocks_from_markdown():
    mod = _load_scanner()
    md = "text\n```python\nimport sqlite3\nsqlite3.connect('x')\n```\nmore\n"
    blocks = mod._extract_python_blocks(md)
    assert "sqlite3.connect" in blocks
