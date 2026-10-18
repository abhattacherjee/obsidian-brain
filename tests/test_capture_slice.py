"""Tests for hooks/capture_slice.py (#425)."""

import json

from capture_slice import count_user_messages, first_user_text, read_slice, render_slice


def _user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _assistant(text, tool=None):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {"command": "pytest -q"}})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _write(path, rows, tail=b""):
    with open(path, "wb") as fh:
        for row in rows:
            fh.write(json.dumps(row).encode() + b"\n")
        fh.write(tail)
    return path


def test_slice_stops_at_last_newline(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("one"), _user("two")], tail=b'{"type": "user", "mess')
    rows, end = read_slice(p, 0)
    assert [r["message"]["content"] for r in rows] == ["one", "two"]
    assert end == p.read_bytes().rfind(b"\n") + 1


def test_slice_resumes_without_overlap(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user(f"m{i}") for i in range(6)])
    first, end1 = read_slice(p, 0, max_bytes=120)
    second, end2 = read_slice(p, end1)
    texts = [r["message"]["content"] for r in first + second]
    assert texts == [f"m{i}" for i in range(6)]
    assert first and second and end2 == p.stat().st_size


def test_slice_spans_compact_boundary(tmp_path):
    rows = [_user("before"), {"type": "system", "subtype": "compact_boundary"}, _user("after")]
    p = _write(tmp_path / "t.jsonl", rows)
    got, _ = read_slice(p, 0)
    text = render_slice(got)
    assert "USER: before" in text and "USER: after" in text
    assert text.index("before") < text.index("compacted") < text.index("after")


def test_slice_caps_max_bytes(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("x" * 1000) for _ in range(1000)])
    assert p.stat().st_size > 1_000_000
    seen, offset, calls = 0, 0, 0
    while offset < p.stat().st_size:
        rows, new = read_slice(p, offset, max_bytes=400_000)
        assert new - offset <= 400_000 and new > offset
        seen, offset, calls = seen + len(rows), new, calls + 1
    assert seen == 1000 and calls >= 3


def test_slice_skips_line_longer_than_window(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("y" * 5000), _user("small")])
    rows, end = read_slice(p, 0, max_bytes=1000)
    assert rows == [] and end > 0
    rows, _ = read_slice(p, end, max_bytes=1000)
    assert [r["message"]["content"] for r in rows] == ["small"]


def test_slice_skips_bad_json_line(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(json.dumps(_user("ok")).encode() + b"\nnot json\n[1]\n")
    rows, end = read_slice(p, 0)
    assert len(rows) == 1 and end == p.stat().st_size


def test_start_beyond_eof_returns_empty(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("a")])
    assert read_slice(p, 10_000) == ([], 10_000)


def test_render_scrubs_secrets_and_skips_meta(tmp_path):
    rows = [
        _user("my key is ghp_" + "a" * 36),
        _user("<system-reminder>ignore</system-reminder>", isMeta=True),
        _assistant("running tests", tool="Bash"),
    ]
    text = render_slice(rows)
    assert "ghp_" + "a" * 36 not in text
    assert "system-reminder" not in text
    assert "ASSISTANT: running tests" in text and "TOOL Bash" in text


def test_render_truncates(tmp_path):
    text = render_slice([_user("z" * 3000) for _ in range(100)], max_chars=10_000)
    assert len(text) < 10_100 and text.endswith("[… slice truncated]")


def test_first_user_text_and_count(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_user("hidden", isMeta=True), _user("SENTINEL hello"), _user("next")])
    assert first_user_text(p) == "SENTINEL hello"
    assert count_user_messages(p) == 2
    assert first_user_text(tmp_path / "missing.jsonl") == ""
    assert count_user_messages(tmp_path / "missing.jsonl") == 0
