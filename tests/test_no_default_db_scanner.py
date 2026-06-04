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


def test_audit_flags_raw_connect_in_extracted_markdown_block():
    mod = _load_scanner()
    md = (
        "Some skill prose.\n\n```python\n"
        "import sqlite3\n\n"
        "def opendb(p):\n    return sqlite3.connect(p)\n"
        "```\n\nmore prose\n"
    )
    blocks = mod._extract_python_blocks(md)
    violations = mod.audit_raw_connect("skills/foo/SKILL.md", blocks)
    assert violations, "raw sqlite3.connect inside an extracted SKILL.md python block must be flagged"


def test_allowlist_is_function_scoped_not_file_scoped():
    mod = _load_scanner()
    # In an allowlisted FILE, only the allowlisted FUNCTION (_connect) may call
    # sqlite3.connect — any other function in that file must still be flagged.
    src = "import sqlite3\n\ndef other(p):\n    return sqlite3.connect(p)\n"
    violations = mod.audit_raw_connect("hooks/vault_index.py", src)
    assert violations, "allowlist must be function-scoped, not file-scoped"


def test_extract_python_blocks_preserves_source_line_numbers():
    mod = _load_scanner()
    md = (
        "# Title\n"                       # line 1
        "prose line\n"                    # line 2
        "```python\n"                     # line 3
        "import sqlite3\n"                 # line 4
        "conn = sqlite3.connect('x')\n"   # line 5
        "```\n"                           # line 6
    )
    blocks = mod._extract_python_blocks(md)
    violations = mod.audit_raw_connect("skills/x/SKILL.md", blocks)
    assert violations, "expected a finding for the raw connect in the python block"
    assert violations[0][0] == 5, f"lineno should map to source line 5, got {violations[0][0]}"


def test_audit_shell_raw_connect_flags_heredoc():
    mod = _load_scanner()
    sh = (
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import sqlite3\n"
        "conn = sqlite3.connect(db)\n"
        "PY\n"
    )
    violations = mod.audit_shell_raw_connect("scripts/dev-test/x.sh", sh)
    assert violations, "raw sqlite3.connect in a shell heredoc must be flagged"
    assert violations[0][0] == 4, f"lineno should be the shell line 4, got {violations[0][0]}"


def test_audit_shell_raw_connect_respects_noqa():
    mod = _load_scanner()
    sh = (
        "python3 - <<'PY'\n"
        "conn = sqlite3.connect(db)  # noqa: vault-db-connect\n"
        "PY\n"
    )
    violations = mod.audit_shell_raw_connect("scripts/dev-test/x.sh", sh)
    assert violations == [], "# noqa: vault-db-connect must suppress the shell finding"
