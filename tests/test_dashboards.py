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

DQL_FILTER = '(!status OR !startswith(lower(string(status)), "provisional"))'
JS_FILTER = '.where(p => !p.status || !String(p.status).toLowerCase().startsWith("provisional"))'
_BLOCK = re.compile(r"^```(dataview|dataviewjs)\n(.*?)^```$", re.M | re.S)


def _blocks(text):
    return [(m.group(1), m.group(2)) for m in _BLOCK.finditer(text)]


def _insight_blocks():
    out = []
    for name in FILES:
        for i, (kind, body) in enumerate(_blocks((DASH / name).read_text())):
            if "claude-insights" in body:
                out.append(pytest.param(kind, body, id=f"{name}#{i}"))
    return out


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
    positive_status = re.search(r'\bstatus = "(?!provisional)', where.group(1))
    assert positive_status or DQL_FILTER in where.group(1), body


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
