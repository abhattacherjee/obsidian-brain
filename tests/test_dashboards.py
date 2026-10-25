"""#458: dashboards over claude-insights leave provisional notes out and cap their rows.

``dashboards/*.md`` is the source; ``skills/obsidian-setup/SKILL.md`` Step 6
carries an inline copy of each file, which must match it exactly.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASH = REPO / "dashboards"
SETUP = REPO / "skills" / "obsidian-setup" / "SKILL.md"
FILES = sorted(p.name for p in DASH.glob("*.md"))
EXPECTED_INSIGHT_BLOCKS = 11  # claude-insights query blocks across dashboards/*.md; update when one is added or removed

DQL_FILTER = '(!status OR !startswith(lower(string(status)), "provisional"))'
JS_FILTER = '.where(p => !p.status || !String(p.status).toLowerCase().startsWith("provisional"))'
_BLOCK = re.compile(r"^```(dataview|dataviewjs)\n(.*?)^```$", re.M | re.S)


def _blocks(text):
    return [(m.group(1), m.group(2)) for m in _BLOCK.finditer(text)]


def _has_positive_status(where):
    """A `status = "<x>"` (x not provisional, any case) AND-joined to the rest of the WHERE."""
    if re.search(r"\bOR\b", where, re.I):
        return False
    if re.search(r"!|\bNOT\b", where, re.I):
        return False  # a negated equality admits provisional notes
    return bool(re.search(r'\bstatus = "(?!provisional)', where, re.I))


def _insight_blocks():
    out = []
    for name in FILES:
        for i, (kind, body) in enumerate(_blocks((DASH / name).read_text())):
            if "claude-insights" in body:
                out.append(pytest.param(kind, body, id=f"{name}#{i}"))
    return out


def test_the_walk_finds_the_expected_insight_blocks():
    # Positive control: an empty walk would skip the parametrized tests instead of failing them.
    expected = sum("claude-insights" in body for n in FILES for _, body in _blocks((DASH / n).read_text()))
    assert expected == EXPECTED_INSIGHT_BLOCKS and len(_insight_blocks()) == expected


@pytest.mark.parametrize("where,ok", [
    ('type = "claude-insight" AND status = "active"', True),
    ('type = "claude-insight" OR status = "active"', False),
    ('status = "Provisional"', False),
    ('status = "provisional"', False),
    ('status = "promoted"', True),
    ('!(status = "active")', False),
    ('!status = "active"', False),
    ('NOT (status = "active")', False),
    ('NOT status = "active"', False),
    ('status != "active"', False),
    ('type = "claude-insight" AND NOT status = "active"', False),
])
def test_positive_status_exemption(where, ok):
    assert _has_positive_status(where) is ok


def test_every_dashboard_has_blocks():
    assert FILES and all(_blocks((DASH / n).read_text()) for n in FILES)


@pytest.mark.parametrize("kind,body", _insight_blocks())
def test_insight_queries_leave_provisional_out(kind, body):
    if kind == "dataviewjs":
        # Every dv.pages over claude-insights is filtered right at its source.
        sources = re.findall(r"dv\.pages\('\"claude-insights\"'\)\s*(.*)", body)
        assert sources and all(s.startswith(JS_FILTER) for s in sources), body
        return
    where = re.search(r"^WHERE (.*)$", body, re.M)
    assert where, body
    if re.fullmatch(r'type = "claude-retro"', where.group(1)):
        return  # auto capture never writes a retro, so no retro is provisional
    assert _has_positive_status(where.group(1)) or DQL_FILTER in where.group(1), body


@pytest.mark.parametrize("kind,body", [p for p in _insight_blocks() if p.values[0] == "dataview"])
def test_insight_list_queries_have_a_limit(kind, body):
    if re.search(r"^GROUP BY ", body, re.M):
        return
    assert re.search(r"^LIMIT \d+$", body, re.M), body


def _inline_copies():
    text = SETUP.read_text()
    pat = re.compile(r"\*\*File: `\$VAULT_PATH/claude-dashboards/([\w.-]+)`\*\*\n\n```markdown\n(.*?)^```$", re.M | re.S)
    return {m.group(1): m.group(2).replace("\\```", "```") for m in pat.finditer(text)}


def test_obsidian_setup_inline_copies_equal_the_dashboard_files():
    copies = _inline_copies()
    assert sorted(copies) == FILES
    for name in FILES:
        assert copies[name] == (DASH / name).read_text(), name
