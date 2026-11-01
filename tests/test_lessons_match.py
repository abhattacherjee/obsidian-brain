"""Document extraction, source-local ranking, and the lesson coverage gate."""
from __future__ import annotations

import hashlib
import math
import os
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
    docs = match.rule_docs(home, ["demo"], workspace, errors)
    expected = {str(p).replace(str(home), "~", 1) for p in (
        global_rule, project_rule, skill, cache / "1.10.0/skills/demo/SKILL.md")}
    assert {d["ref"] for d in docs} == expected
    assert all(d["source"] == "rule" and d["line"] == 1 for d in docs)
    assert errors == []


def test_rule_docs_never_read_a_project_name_that_leaves_the_workspace(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    # real files behind each bad name: without the guard they WOULD be read
    write(tmp_path / "escape/CLAUDE.md", "- Escaped via dotdot\n")
    write(workspace / "a/b/CLAUDE.md", "- Separator name\n")
    write(workspace / "bad..name/CLAUDE.md", "- Dotdot inside the name\n")
    write(workspace / "back\\slash/CLAUDE.md", "- Backslash name\n")
    errors = []
    docs = match.rule_docs(home, ["../escape", "a/b", "bad..name", "back\\slash"], workspace, errors)
    assert docs == [] and errors == []


def test_rule_docs_containment_check_rejects_a_symlink_out_of_the_workspace(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    write(tmp_path / "outside/CLAUDE.md", "- Outside the workspace\n")
    (workspace / "linked").symlink_to(tmp_path / "outside")
    write(workspace / "inside/CLAUDE.md", "- Inside\n")
    docs = match.rule_docs(home, ["linked", "inside"], workspace, [])
    assert [d["text"] for d in docs] == ["Inside"]


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


def _naive_best(query, sources):
    """The straightforward ranking: re-tokenise and re-weigh every document for each query."""
    import tfidf
    from collections import Counter
    qt = tfidf._tokenize_for_tfidf(query)
    best = None
    for source, docs in sources.items():
        toks = [tfidf._tokenize_for_tfidf(d["text"]) for d in docs]
        df = Counter(t for doc in toks for t in set(doc))

        def vec(tokens):
            w = {t: (1 + math.log(c)) * (1 + math.log((len(docs) + 1) / (df[t] + 1)))
                 for t, c in Counter(tokens).items()}
            n = math.sqrt(sum(x * x for x in w.values()))
            return {t: x / n for t, x in w.items()} if n else {}

        q = vec(qt)
        for d, t in zip(docs, toks):
            score = sum(x * q.get(term, 0) for term, x in vec(t).items())
            key = (source, d["ref"])
            if score > 0 and (best is None or score > best[1] or (score == best[1] and key < best[2])):
                best = (d, score, key)
    return (best[0], best[1]) if best else None


def test_index_ranks_like_the_naive_scan_and_can_be_reused():
    words = "router refusal script session grep caller preflight commit chain issue memory rule".split()
    import random
    rng = random.Random(7)

    def docs(prefix, n):
        return [{"ref": f"{prefix}{i}", "text": " ".join(rng.choices(words, k=rng.randint(2, 9)))}
                for i in range(n)]

    sources = {"issue": docs("i", 9), "memory": docs("m", 12), "rule": docs("r", 7)}
    indexes = {name: match.Index(items) for name, items in sources.items()}
    for _ in range(40):
        query = " ".join(rng.choices(words, k=rng.randint(1, 6)))
        expected = _naive_best(query, sources)
        for given in (sources, indexes):  # lists, and prebuilt indexes reused across queries
            got = match.best_candidate(query, given)
            assert (got is None) == (expected is None)
            if expected:
                assert got[0] is expected[0] and got[1] == pytest.approx(expected[1])


def test_view_limits_documents_but_keeps_the_whole_sources_idf():
    docs = [{"ref": "a", "text": "alpha beta"}, {"ref": "b", "text": "alpha gamma"},
            {"ref": "c", "text": "delta"}]
    index = match.Index(docs)
    whole = index.top(["alpha"])
    assert whole[0]["ref"] == "a"  # tie on score: lowest ref
    only_b = match.best_candidate("alpha", {"memory": index.view({1, 2})})
    assert only_b[0]["ref"] == "b"
    # idf is the whole source's (3 documents), not the view's (2): the score matches the full index's
    assert only_b[1] == pytest.approx(index.top(["alpha"], {1})[1])
    # independently: a document of {alpha, gamma} against the query "alpha", idf over 3 documents
    # (df alpha=2, gamma=1, count 3): weights alpha 1+ln(4/3), gamma 1+ln(4/2); cosine on alpha only
    alpha, gamma = 1 + math.log(4 / 3), 1 + math.log(4 / 2)
    expected = alpha / math.hypot(alpha, gamma)
    assert expected == pytest.approx(0.6053, abs=1e-4)
    assert only_b[1] == pytest.approx(expected)
    # recomputed over the two allowed documents only (df alpha=2, gamma=1, count 2) it would be 0.7071
    assert only_b[1] != pytest.approx(0.7071, abs=1e-3)
    assert match.best_candidate("alpha", {"memory": index.view({2})}) is None


def test_tilde_fences_hide_their_lines_too(tmp_path):
    text = "Visible rule " * 8
    path = write(tmp_path / "SKILL.md", text + "\n\n~~~\n" + "hidden " * 30 + "\n~~~\n\n" + text + "again")
    assert [p for _, p in match.rule_paragraphs(path)] == [text.strip(), text + "again"]
    bullets = write(tmp_path / "CLAUDE.md", "- Shown\n~~~\n- Hidden\n~~~\n- Also shown\n")
    assert [b for _, b in match.rule_bullets(bullets)] == ["Shown", "Also shown"]


def test_gate_prompt_fills_each_placeholder_once():
    seen = {}

    def runner(prompt):
        seen["p"] = prompt
        return '{"covers": true}'

    candidate = {"source": "memory", "text": "cand <<A>> <<S>>"}
    assert match.gate("lesson <<C>> <<S>>", candidate, runner) is True
    # text that looks like a placeholder is data, not a second substitution
    assert "lesson <<C>> <<S>>" in seen["p"] and "(memory) cand <<A>> <<S>>" in seen["p"]


def test_fence_closes_only_on_the_same_character_and_enough_of_it(tmp_path):
    text = "Visible rule " * 8
    hidden = "hidden " * 30
    # a four-backtick fence is not closed by a three-backtick line
    path = write(tmp_path / "A.md", text + "\n\n````\n```\n" + hidden + "\n```\n" + hidden + "\n````\n\n" + text + "again")
    assert [p for _, p in match.rule_paragraphs(path)] == [text.strip(), text + "again"]
    # a tilde fence is not closed by backticks
    path = write(tmp_path / "B.md", text + "\n\n~~~\n" + hidden + "\n```\n" + hidden + "\n~~~\n\n" + text + "again")
    assert [p for _, p in match.rule_paragraphs(path)] == [text.strip(), text + "again"]
    # a closing fence may be longer than the opening one, but not shorter
    path = write(tmp_path / "C.md", "- Shown\n````\n- Hidden\n```\n- Still hidden\n`````\n- Also shown\n")
    assert [b for _, b in match.rule_bullets(path)] == ["Shown", "Also shown"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("blocked", [".claude/skills", ".claude/plugins/cache",
                                     ".claude/plugins/cache/market", ".claude/plugins/cache/market/plugin/1.0.0/skills"])
def test_rule_docs_report_an_unreadable_directory(tmp_path, blocked):
    home = tmp_path / "home"
    write(home / ".claude/skills/local/SKILL.md", "Local rule " * 10)
    write(home / ".claude/plugins/cache/market/plugin/1.0.0/skills/demo/SKILL.md", "Plugin rule " * 10)
    target = home / blocked
    target.chmod(0)
    errors = []
    try:
        match.rule_docs(home, [], tmp_path, errors)
    finally:
        target.chmod(0o700)
    assert [Path(e["path"]) for e in errors] == [target]


def test_rule_docs_missing_directories_are_not_errors(tmp_path):
    errors = []
    assert match.rule_docs(tmp_path / "home", [], tmp_path, errors) == [] and errors == []
