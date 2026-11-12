"""Every headless `claude -p` argv in hooks/ and scripts/ must carry --no-session-persistence (#479).

Without the flag each run saves a session file that later shows up as a session.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG = "--no-session-persistence"
HEADLESS = {"-p", "--print"}


def sites_in(source: str, name: str = "<src>") -> list[tuple[str, list]]:
    """Each list literal that starts with "claude" and holds -p or --print anywhere."""
    sites = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.List) and node.elts:
            values = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            if values[0] == "claude" and HEADLESS & {v for v in values if isinstance(v, str)}:
                sites.append((f"{name}:{node.lineno}", values))
    return sites


def claude_p_sites():
    sites = []
    for path in sorted([*(ROOT / "hooks").glob("*.py"), *(ROOT / "scripts").glob("*.py")]):
        sites += sites_in(path.read_text(encoding="utf-8"), path.name)
    return sites


def test_found_enough_claude_p_sites():
    assert len(claude_p_sites()) >= 5


def test_every_claude_p_argv_disables_session_persistence():
    missing = [where for where, values in claude_p_sites() if FLAG not in values]
    assert missing == [], f"claude -p without {FLAG}: {missing}"


def test_scanner_finds_reordered_and_long_form_headless_argvs():
    for argv in ('["claude", "--model", m, "-p"]', '["claude", "--print", "--model", m]'):
        found = sites_in(f"subprocess.run({argv}, input=prompt)")
        assert len(found) == 1, argv
        assert FLAG not in found[0][1]
    assert sites_in('subprocess.run(["claude", "--version"])') == []
