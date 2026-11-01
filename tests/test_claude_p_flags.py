"""Every headless `claude -p` argv in hooks/ and scripts/ must carry --no-session-persistence (#479).

Without the flag each run saves a session file that later shows up as a session.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG = "--no-session-persistence"


def claude_p_sites():
    sites = []
    for path in sorted([*(ROOT / "hooks").glob("*.py"), *(ROOT / "scripts").glob("*.py")]):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and len(node.elts) >= 2:
                first = [e.value if isinstance(e, ast.Constant) else None for e in node.elts[:2]]
                if first == ["claude", "-p"]:
                    values = [e.value for e in node.elts if isinstance(e, ast.Constant)]
                    sites.append((f"{path.name}:{node.lineno}", values))
    return sites


def test_found_enough_claude_p_sites():
    assert len(claude_p_sites()) >= 8


def test_every_claude_p_argv_disables_session_persistence():
    missing = [where for where, values in claude_p_sites() if FLAG not in values]
    assert missing == [], f"claude -p without {FLAG}: {missing}"
