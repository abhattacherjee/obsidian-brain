"""Retro item identity, corpus loading, and sparse similarity candidates."""
import hashlib
import random
import time

import pytest

from lessons_corpus import item_key, load_items, top_candidates


def write_note(folder, name, body, metadata=None, eol="\n"):
    folder.mkdir(parents=True, exist_ok=True)
    header = ("type: claude-retro\ndate: 2026-10-01\nproject: demo\n"
              if metadata is None else metadata)
    text = "---\n" + header + "---\n\n## Process Improvements\n" + body
    path = folder / name
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))
    return path


def test_item_key_ignores_trace_and_checkbox():
    stem = "2026-10-01-retro-ab12"
    assert item_key(stem, "- [ ] Run X first.") == item_key(
        stem, "- [x] Run X first. → memory `feedback_x`")
    assert item_key(stem, "- [X] RUN\n  X\tfirst. → `docs/x.md`") == (
        f"{stem}:" + hashlib.sha1(b"run x first.").hexdigest()[:16])


def test_item_key_keeps_prose_arrow():
    assert item_key("retro", "- [ ] A → B") != item_key("retro", "- [ ] A")
    assert item_key("retro", "- [ ] A → B → memory `x`") == item_key(
        "retro", "- [ ] A → B")
    # Only the last line can carry the item's trace.
    assert item_key("retro", "- [ ] A → memory `x`\n  B") != item_key(
        "retro", "- [ ] A\n  B")


def test_load_items_reads_all_items_ticked_and_traced(tmp_path):
    folder = tmp_path / "insights"
    first = write_note(folder, "2026-10-01-retro-a.md",
                       "- [ ] Run X first.\n- [x] A → B\n"
                       "  Keep Detail. → memory `feedback_x`\n")
    second = write_note(folder, "2026-10-02-retro-b.md",
                        "- [X] File fix. → [o/r#5](https://github.com/o/r/issues/5)\n",
                        'type: claude-retro\ndate: "2026-09-30T12:00:00Z"\nproject: \'other\'\n')
    third = write_note(folder, "2026-10-03-retro-c.md",
                       "- [ ] Already covered. → skipped: duplicate\n",
                       "type: claude-retro\n")
    items, skipped = load_items(str(tmp_path), "insights")
    assert skipped == []
    assert len(items) == 4
    assert items[0] == {
        "key": item_key(first.stem, "- [ ] Run X first."),
        "note": first.stem, "path": str(first), "item": 1, "line": 8,
        "project": "demo", "date": "2026-10-01", "text": "Run X first.",
        "checked": False, "trace": None,
    }
    assert items[1]["item"] == 2
    assert items[1]["line"] == 9
    assert items[1]["text"] == "A → B Keep Detail."
    assert items[1]["checked"] is True
    assert items[1]["trace"] == {"kind": "memory", "stem": "feedback_x", "note": None}
    assert items[2]["path"] == str(second)
    assert items[2]["date"] == "2026-09-30"
    assert items[2]["project"] == "other"
    assert items[2]["checked"] is True
    assert items[2]["trace"]["ref"] == "o/r#5"
    assert items[3]["path"] == str(third)
    assert items[3]["date"] == "2026-10-03"
    assert items[3]["project"] == "unknown"
    assert items[3]["trace"] == {"kind": "skipped", "reason": "duplicate"}


def test_load_items_skips_invalid_frontmatter_with_reason(tmp_path):
    folder = tmp_path / "insights"
    folder.mkdir()
    bad = folder / "2026-10-01-retro-bad.md"
    bad.write_text("---\ntype: claude-retro\n## Process Improvements\n- [ ] X\n")
    binary = folder / "2026-10-02-retro-binary.md"
    binary.write_bytes(b"\xff")
    items, skipped = load_items(str(tmp_path), "insights")
    assert items == []
    assert skipped == [{"path": str(p), "reason": "unreadable"} for p in (bad, binary)]


def test_load_items_crlf_note_matches_lf_keys(tmp_path):
    body = "- [ ] Run First.\n  Keep Detail. → memory `x`\n- [X] A → B\n"
    write_note(tmp_path / "insights", "2026-10-01-retro-lf.md", body)
    write_note(tmp_path / "insights", "2026-10-02-retro-crlf.md", body, eol="\r\n")
    items, skipped = load_items(str(tmp_path), "insights")
    assert skipped == []
    assert [i["key"].split(":")[1] for i in items[:2]] == [
        i["key"].split(":")[1] for i in items[2:]]
    assert [i["text"] for i in items[:2]] == [i["text"] for i in items[2:]]


def test_load_items_ignores_subfolders_and_non_retro_files(tmp_path):
    folder = tmp_path / "insights"
    keep = write_note(folder, "2026-10-01-retro-a.md", "- [ ] Keep.\n")
    write_note(folder / "lessons", "2026-10-01-retro-hidden.md", "- [ ] Hidden.\n")
    write_note(folder, "2026-10-01-insight-a.md", "- [ ] Hidden.\n")
    items, skipped = load_items(str(tmp_path), "insights")
    assert [i["path"] for i in items] == [str(keep)]
    assert skipped == []


def test_load_items_skips_paths_resolving_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    folder = vault / "insights"
    folder.mkdir(parents=True)
    outside = write_note(tmp_path / "outside", "2026-10-01-retro-a.md", "- [ ] Hidden.\n")
    (folder / outside.name).symlink_to(outside)
    assert load_items(str(vault), "insights") == ([], [])


def test_load_items_reports_oserror(tmp_path, monkeypatch):
    path = write_note(tmp_path / "insights", "2026-10-01-retro-a.md", "- [ ] X\n")

    def fail(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr("builtins.open", fail)
    assert load_items(str(tmp_path), "insights") == (
        [], [{"path": str(path), "reason": "unreadable"}])


def candidate_items():
    return [
        {"key": "anchor", "note": "one", "text": "alpha beta"},
        {"key": "same-note", "note": "one", "text": "alpha beta"},
        {"key": "z", "note": "two", "text": "alpha beta"},
        {"key": "a", "note": "three", "text": "alpha beta"},
        {"key": "partial", "note": "four", "text": "alpha gamma"},
        {"key": "zero", "note": "five", "text": "delta"},
        {"key": "empty", "note": "six", "text": "the and"},
    ]


def test_top_candidates_excludes_same_note_and_is_deterministic():
    items = candidate_items()
    result = top_candidates(items, k=2)
    assert result["anchor"] == ["a", "z"]
    assert result["zero"] == result["empty"] == []
    assert top_candidates(list(reversed(items)), k=2) == result
    assert top_candidates(items, k=10)["anchor"] == ["a", "z", "partial"]


def test_top_candidates_only_limits_anchors():
    items = candidate_items()
    assert top_candidates(items, only={"anchor", "missing"}) == {
        "anchor": top_candidates(items)["anchor"]}
    assert top_candidates(items, only=set()) == {}


def test_top_candidates_uses_term_frequency_and_corpus_document_frequency():
    # Repeated alpha raises its TF, while beta's rarity raises its IDF.
    items = [
        {"key": "anchor", "note": "one", "text": "alpha alpha beta"},
        {"key": "alpha", "note": "two", "text": "alpha"},
        {"key": "beta", "note": "three", "text": "beta"},
        {"key": "exact", "note": "four", "text": "alpha alpha beta"},
    ]
    items.extend({"key": f"filler{i}", "note": "one", "text": "alpha"}
                 for i in range(30))
    # The other anchor-note items still contribute to corpus DF. Beta's
    # IDF now exceeds twice alpha's, so beta outranks alpha despite its TF.
    assert top_candidates(items, only={"anchor"}) == {
        "anchor": ["exact", "beta", "alpha"]}


@pytest.mark.parametrize("k", [0, -1])
def test_top_candidates_nonpositive_k(k):
    assert top_candidates(candidate_items(), k=k) == {
        item["key"]: [] for item in candidate_items()}


def test_top_candidates_empty_corpus():
    assert top_candidates([]) == {}


def test_top_candidates_speed_4000_items():
    rng = random.Random(471)
    vocabulary = [f"word{i}" for i in range(2000)]
    items = [{"key": f"item{i:04d}", "note": f"note{i // 10}",
              "text": " ".join(rng.choices(vocabulary, k=25))}
             for i in range(4000)]
    start = time.perf_counter()
    result = top_candidates(items)
    elapsed = time.perf_counter() - start
    assert len(result) == 4000
    assert all(len(candidates) == 10 for candidates in result.values())
    assert elapsed < 10, f"candidate search took {elapsed:.2f}s"


@pytest.mark.parametrize("header", [
    'type: claude-retro\ndate: ""\nproject: demo\n',
    "type: claude-retro\ndate: sometime last week\nproject: demo\n",
    "type: claude-retro\ndate: 2026-13-45\nproject: demo\n",
    "type: claude-retro\nproject: demo\n",
])
def test_load_items_bad_date_falls_back_to_file_name(tmp_path, header):
    # Dates are compared as strings and split into ints for ranking: only ISO dates may pass.
    write_note(tmp_path / "insights", "2026-10-05-retro-x1.md", "- [ ] A\n", metadata=header)
    items, skipped = load_items(str(tmp_path), "insights")
    assert [i["date"] for i in items] == ["2026-10-05"] and skipped == []


def test_load_items_bad_date_and_bad_file_name_is_unreadable(tmp_path):
    path = write_note(tmp_path / "insights", "x-retro-x1.md", "- [ ] A\n",
                      metadata="type: claude-retro\ndate: soon\nproject: demo\n")
    items, skipped = load_items(str(tmp_path), "insights")
    assert items == [] and skipped == [{"path": str(path), "reason": "no valid date"}]


def test_load_items_keeps_a_valid_frontmatter_date(tmp_path):
    write_note(tmp_path / "insights", "2026-10-05-retro-x1.md", "- [ ] A\n",
               metadata="type: claude-retro\ndate: 2026-10-07T09:00\nproject: demo\n")
    assert [i["date"] for i in load_items(str(tmp_path), "insights")[0]] == ["2026-10-07"]
