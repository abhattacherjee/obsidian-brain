"""Document extraction, source-local ranking, and the lesson coverage gate."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

import lessons_match as match


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_lesson_text_and_anchor_shapes():
    members = [f"Member {i}" for i in range(7)]
    assert match.lesson_text("Name", members) == "Name " + " ".join(members)
    assert match.anchor_text("Name", members) == "Name\n" + "\n".join(
        f"- {text}" for text in members[:5])
    assert match.lesson_text("Name", []) == "Name"
    assert match.anchor_text("Name", []) == "Name"
    assert match.short_hash("é") == hashlib.sha1("é".encode()).hexdigest()[:12]


def test_memory_docs_skip_index_and_report_unreadable(tmp_path):
    index = write(tmp_path / "MEMORY.md", "Index")
    note = write(tmp_path / "feedback.md",
                 '---\nname: "Useful name"\ndescription: Useful description\ntype: feedback\n---\nBody')
    plain = write(tmp_path / "plain.md", "x" * 2100)
    missing = tmp_path / "missing.md"
    binary = tmp_path / "binary.md"
    binary.write_bytes(b"\xff")
    errors = []
    docs = match.memory_docs([str(p) for p in (index, note, plain, missing, binary)], errors)
    assert docs[0] == {"source": "memory", "ref": "feedback", "path": str(note),
                       "text": "Useful name Useful description Body"}
    assert docs[1]["text"] == "x" * 2000
    assert [e["path"] for e in errors] == [str(missing), str(binary)]
    assert all(e["error"] for e in errors)


def test_rule_bullets_join_continuations(tmp_path):
    path = write(tmp_path / "CLAUDE.md",
                 "Heading\n- First\n  continuation\n\tmore\n- Second\n\n"
                 "```md\n- Hidden\n```\n  - Nested alone\n- Last\n")
    assert match.rule_bullets(path) == [(2, "First continuation more"),
                                        (5, "Second"), (11, "Last")]


def test_rule_paragraphs_skip_fences_and_short(tmp_path):
    first = "First rule " * 8
    second = "Second rule " * 8
    path = write(tmp_path / "SKILL.md", "Short\n\n" + first + "\ncontinued\n\n"
                 "```python\n" + "hidden " * 30 + "\n```\n" + second)
    assert match.rule_paragraphs(path) == [(3, first.strip() + " continued"),
                                           (9, second.strip())]


def test_unclosed_fence_stops_paragraphs(tmp_path):
    text = "Visible rule " * 8
    path = write(tmp_path / "SKILL.md", text + "\n```\n" + "hidden " * 50)
    assert match.rule_paragraphs(path) == [(1, text.strip())]


def test_rule_docs_pick_highest_plugin_version(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    global_rule = write(home / ".claude/CLAUDE.md", "- Global\n")
    project_rule = write(workspace / "demo/CLAUDE.md", "- Project\n")
    skill = write(home / ".claude/skills/local/SKILL.md", "Local rule " * 10)
    cache = home / ".claude/plugins/cache/market/plugin"
    for version in ("1.9.0", "1.10.0", "latest", "2.0-beta"):
        write(cache / version / "skills/demo/SKILL.md", version + " rule" * 30)
    errors = []
    docs = match.rule_docs(home, ["demo", "../escape", "a/b", "bad..name"], workspace, errors)
    expected = {str(p).replace(str(home), "~", 1) for p in (
        global_rule, project_rule, skill, cache / "1.10.0/skills/demo/SKILL.md")}
    assert {d["ref"] for d in docs} == expected
    assert all(d["source"] == "rule" and d["line"] == 1 for d in docs)
    assert errors == []


def test_rule_docs_skip_missing_and_report_read_errors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    bad = write(home / ".claude/skills/bad/SKILL.md", "bad")
    bad.write_bytes(b"\xff")
    blocked = write(home / ".claude/CLAUDE.md", "- Blocked")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("blocked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    errors = []
    assert match.rule_docs(home, ["missing"], tmp_path, errors) == []
    assert {e["path"] for e in errors} == {str(bad), str(blocked)}


def test_issue_docs_skip_bad_numbers():
    docs = match.issue_docs("owner/repo", [
        {"number": 7, "title": "Title", "body": "Body", "createdAt": "2026-10-01T12:00:00Z", "state": "OPEN"},
        {"number": 8, "title": "Long", "body": "x" * 2500, "state": "CLOSED"},
        {}, {"number": "9"}, {"number": True},
    ])
    assert docs[0] == {"source": "issue", "ref": "owner/repo#7", "text": "Title\nBody",
                       "created": "2026-10-01", "state": "OPEN"}
    assert len(docs) == 2
    assert docs[1]["created"] is None
    assert docs[1]["text"] == ("Long\n" + "x" * 2500)[:2000]


def test_best_candidate_picks_highest_across_sources_and_breaks_ties_by_ref():
    weak = {"ref": "weak", "text": "router testing"}
    best = {"ref": "best", "text": "router refusal"}
    assert match.best_candidate("router refusal", {"issue": [weak], "memory": [best]}) == (best, pytest.approx(1))
    low = {"ref": "a", "text": "router refusal"}
    assert match.best_candidate("router refusal", {"rule": [low], "memory": [best, low]})[0] is low
    issue = dict(low)
    assert match.best_candidate("router refusal", {"memory": [low], "issue": [issue]})[0] is issue


def test_best_candidate_uses_source_local_idf_and_sublinear_tf():
    doc = {"ref": "a", "text": "alpha alpha beta"}
    filler = {"ref": "b", "text": "beta gamma"}
    _, score = match.best_candidate("alpha", {"memory": [doc, filler]})
    alpha = (1 + math.log(2)) * (1 + math.log(3 / 2))
    assert score == pytest.approx(alpha / math.sqrt(alpha ** 2 + 1))
    assert match.best_candidate("alpha", {"memory": [doc], "rule": [filler]})[1] == pytest.approx(
        (1 + math.log(2)) / math.sqrt((1 + math.log(2)) ** 2 + 1))


def test_best_candidate_none_when_all_scores_zero():
    for query in ("unrelated", "the and", ""):
        assert match.best_candidate(query, {"memory": [{"ref": "a", "text": "router"}], "rule": []}) is None
    assert match.best_candidate("router", {}) is None


@pytest.mark.parametrize("stdout, expected", [
    ('{"covers": true}', True), ('Prose {"covers": false} trailing', False),
    ('{"covers": "yes"}', None), (None, None), ("[]", None),
    ('{"covers": 1}', None), ('{"other": true}', None),
    ('{bad} {"covers": true}', None),
])
def test_parse_gate_accepts_only_bool_covers(stdout, expected):
    assert match.parse_gate(stdout) is expected


def test_gate_retries_once_then_none():
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return "invalid"

    candidate = {"source": "memory", "text": "x" * 900}
    assert match.gate("Anchor {braces}", candidate, runner) is None
    assert len(prompts) == 2
    assert prompts[0] == match.GATE_PROMPT.replace("<<A>>", "Anchor {braces}").replace("<<S>>", "memory").replace("<<C>>", "x" * 800)
    replies = iter([None, '{"covers": false}'])
    assert match.gate("Anchor", candidate, lambda _: next(replies)) is False


@pytest.mark.parametrize("source, state, label", [
    ("issue", "OPEN", "issue, open"), ("issue", "CLOSED", "issue, closed"),
    ("issue", None, "issue"), ("rule", None, "rule"),
])
def test_gate_source_label_and_success_without_retry(source, state, label):
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return '{"covers": true}'

    assert match.gate("Anchor", {"source": source, "state": state, "text": "Candidate"}, runner) is True
    assert len(prompts) == 1
    assert f"({label}) Candidate" in prompts[0]


def test_gate_prompt_holds_the_examples_and_reply_shape():
    assert match.RUBRIC_VERSION == "m4-2026-11-01"
    examples = [
        '1. Lesson: "Write ~/ro.sh at the first router refusal in a session." Candidate (memory): "Router override: on the first refusal, write the per-session ~/ro-<id4>.sh script once and tell the user." -> {"covers": true} (same rule).',
        '2. Lesson: "Filing a follow-up issue should include the failing command output." Candidate (issue, open): "Bug issues must include the exact failing command and its output in the body." -> {"covers": true} (the issue already requires it).',
        '3. Lesson: "Verify every file:line citation in a fix spec before dispatching." Candidate (rule): "Mutation-test every new guard: delete it and expect red." -> {"covers": false} (both about verification, different rules).',
    ]
    assert all(example in match.GATE_PROMPT for example in examples)
    assert match.GATE_PROMPT.endswith('{"covers": true|false}')
