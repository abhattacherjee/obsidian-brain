"""Tests for deep_analysis_pipeline() and build_deep_presentation()."""

import json
import os
from unittest.mock import patch

import pytest

import open_item_dedup
import vault_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_note(path, frontmatter: dict, body: str = ""):
    lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _make_session_note(sessions_dir, name, project, open_items=None, body=""):
    """Create a session note with optional open items."""
    path = sessions_dir / name
    fm = {
        "type": "claude-session",
        "date": "2026-04-10",
        "project": project,
        "status": "summarized",
        "tags": ["claude/session", f"claude/project/{project}"],
    }
    open_section = ""
    if open_items:
        open_section = "\n## Open Questions / Next Steps\n"
        for item in open_items:
            open_section += f"- [ ] {item}\n"
    full_body = body + open_section
    return _write_note(path, fm, full_body)


# ---------------------------------------------------------------------------
# CONSOL_01: Semantic dedup groups similar items
# ---------------------------------------------------------------------------


class TestDeepAnalysisPipeline:
    def test_pipeline_returns_structured_json(self, tmp_vault):
        """Pipeline output contains expected top-level keys."""
        sessions_dir = tmp_vault / "claude-sessions"
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            open_items=["Fix the login handler"],
        )

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(open_item_dedup, "_resolve_project_paths", return_value={}):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["proj"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        assert result.startswith("OK:")
        assert os.path.isfile(output_path)

        data = json.loads(open(output_path).read())
        assert "link_suggestions" in data
        assert "merge_suggestions" in data
        assert "items" in data
        assert "evidence" in data

    def test_semantic_dedup_groups_similar_items(self, tmp_vault):
        """CONSOL_01: Two similar open items should be grouped together."""
        sessions_dir = tmp_vault / "claude-sessions"
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            open_items=["Fix login handler in src/auth.py for PR #99"],
        )
        _make_session_note(
            sessions_dir, "2026-04-10-proj-bbbb.md", "proj",
            open_items=["Fix login handler in src/auth.py for PR #99"],
        )

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(open_item_dedup, "_resolve_project_paths", return_value={}):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["proj"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        data = json.loads(open(output_path).read())
        # Should have found duplicates → group_count >= 1
        assert data["items"]["group_count"] >= 1

    def test_no_open_items_graceful(self, tmp_vault):
        """CONSOL_12: Zero open items should not crash."""
        sessions_dir = tmp_vault / "claude-sessions"
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            body="## Summary\nNothing to do.\n",
        )

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(open_item_dedup, "_resolve_project_paths", return_value={}):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["proj"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        assert result.startswith("OK:")
        data = json.loads(open(output_path).read())
        assert data["items"]["total_raw"] == 0
        assert data["items"]["group_count"] == 0

    def test_already_linked_excluded(self, tmp_vault):
        """LINK_02: Notes that already link to each other should not be suggested."""
        sessions_dir = tmp_vault / "claude-sessions"
        insights_dir = tmp_vault / "claude-insights"
        # Note A links to Note B via wikilink
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            body="## Summary\nWorked on frobulator. See [[2026-04-10-proj-bbbb]]\n",
        )
        _make_session_note(
            sessions_dir, "2026-04-10-proj-bbbb.md", "proj",
            body="## Summary\nAlso worked on frobulator widget.\n",
        )

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(open_item_dedup, "_resolve_project_paths", return_value={}):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["proj"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        data = json.loads(open(output_path).read())
        # The already-linked pair should not appear in link_suggestions
        for suggestion in data["link_suggestions"]:
            pair = {suggestion["note_a"], suggestion["note_b"]}
            assert not (
                "2026-04-10-proj-aaaa" in str(pair)
                and "2026-04-10-proj-bbbb" in str(pair)
            ), "Already-linked notes should not be suggested"


# ---------------------------------------------------------------------------
# Orphan detection tests
# ---------------------------------------------------------------------------


class TestBuildDeepPresentation:
    def test_orphan_detected(self, tmp_vault):
        """ORPHAN_01: A note not linked by any other note is flagged as orphan."""
        sessions_dir = tmp_vault / "claude-sessions"
        insights_dir = tmp_vault / "claude-insights"
        # Note A: no wikilinks pointing to it from other notes
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            body="## Summary\nDid some solo work.\n",
        )
        # Note B: also no links
        _make_session_note(
            sessions_dir, "2026-04-10-proj-bbbb.md", "proj",
            body="## Summary\nAnother session.\n",
        )

        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]
        basenames_json = json.dumps(basenames)

        # Create minimal pipeline JSON
        pipeline_path = str(tmp_vault / "pipeline.json")
        with open(pipeline_path, "w") as f:
            json.dump({
                "link_suggestions": [],
                "merge_suggestions": [],
                "items": {"total_raw": 0, "groups": [], "group_count": 0},
                "evidence": {},
            }, f)

        # Create minimal classifications JSON
        classifications_path = str(tmp_vault / "classifications.json")
        with open(classifications_path, "w") as f:
            json.dump({}, f)

        result = open_item_dedup.build_deep_presentation(
            pipeline_path=pipeline_path,
            classifications_path=classifications_path,
            basenames_json=basenames_json,
            vault_path=str(tmp_vault),
            sessions_folder="claude-sessions",
            insights_folder="claude-insights",
        )

        assert "Orphan" in result or "orphan" in result.lower()
        # At least one note should be flagged
        assert "2026-04-10-proj-aaaa" in result or "2026-04-10-proj-bbbb" in result

    def test_standup_notes_excluded_from_orphans(self, tmp_vault):
        """ORPHAN_03: Standup-type notes should not appear as orphans."""
        sessions_dir = tmp_vault / "claude-sessions"
        # Regular note — could be orphan
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            body="## Summary\nSolo work.\n",
        )
        # Standup note — should be excluded
        standup_path = sessions_dir / "2026-04-10-standup.md"
        _write_note(standup_path, {
            "type": "claude-standup",
            "date": "2026-04-10",
            "tags": ["claude/standup"],
        }, "## Daily Standup\nDid stuff.\n")

        # Emerge note — should also be excluded
        emerge_path = sessions_dir / "2026-04-10-emerge.md"
        _write_note(emerge_path, {
            "type": "claude-emerge",
            "date": "2026-04-10",
            "tags": ["claude/emerge"],
        }, "## Emerge\nThemes.\n")

        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]
        basenames_json = json.dumps(basenames)

        pipeline_path = str(tmp_vault / "pipeline.json")
        with open(pipeline_path, "w") as f:
            json.dump({
                "link_suggestions": [],
                "merge_suggestions": [],
                "items": {"total_raw": 0, "groups": [], "group_count": 0},
                "evidence": {},
            }, f)

        classifications_path = str(tmp_vault / "classifications.json")
        with open(classifications_path, "w") as f:
            json.dump({}, f)

        result = open_item_dedup.build_deep_presentation(
            pipeline_path=pipeline_path,
            classifications_path=classifications_path,
            basenames_json=basenames_json,
            vault_path=str(tmp_vault),
            sessions_folder="claude-sessions",
            insights_folder="claude-insights",
        )

        # Standup and emerge notes should NOT appear in orphan list
        assert "2026-04-10-standup" not in result
        assert "2026-04-10-emerge" not in result


# ---------------------------------------------------------------------------
# _resolve_project_paths tests
# ---------------------------------------------------------------------------


class TestResolveProjectPaths:
    def test_returns_git_repos(self, tmp_path, monkeypatch):
        """Directories with .git are returned."""
        scan_dir = tmp_path / "dev" / "claude_workspace"
        scan_dir.mkdir(parents=True)
        repo = scan_dir / "my-project"
        repo.mkdir()
        (repo / ".git").mkdir()
        non_repo = scan_dir / "plain-dir"
        non_repo.mkdir()

        monkeypatch.setattr(os.path, "expanduser", lambda x: str(tmp_path) if x == "~" else x)
        result = open_item_dedup._resolve_project_paths()
        assert "my-project" in result
        assert "plain-dir" not in result

    def test_handles_missing_dirs(self, tmp_path, monkeypatch):
        """Missing scan directories are skipped gracefully."""
        monkeypatch.setattr(os.path, "expanduser", lambda x: str(tmp_path / "nonexistent") if x == "~" else x)
        result = open_item_dedup._resolve_project_paths()
        assert result == {}


# ---------------------------------------------------------------------------
# Pipeline with evidence (mocked subprocess)
# ---------------------------------------------------------------------------


class TestPipelineEvidence:
    def test_evidence_gathered_with_repo(self, tmp_vault):
        """When a project has a repo path, evidence is gathered."""
        sessions_dir = tmp_vault / "claude-sessions"
        _make_session_note(
            sessions_dir, "2026-04-10-myproj-aaaa.md", "myproj",
            open_items=["Fix the widget"],
        )

        # Create a fake repo
        fake_repo = tmp_vault / "repos" / "myproj"
        fake_repo.mkdir(parents=True)
        (fake_repo / ".git").mkdir()
        (fake_repo / "CHANGELOG.md").write_text("# Changelog\n## v1.0.0\n- Initial\n")

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        import vault_index
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(
            open_item_dedup, "_resolve_project_paths",
            return_value={"myproj": str(fake_repo)},
        ):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["myproj"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        data = json.loads(open(output_path).read())
        # Even if git log fails (no actual repo), changelog should be read
        assert "myproj" in data["evidence"]
        assert "changelog_excerpt" in data["evidence"]["myproj"]


# ---------------------------------------------------------------------------
# build_deep_presentation with rich data
# ---------------------------------------------------------------------------


class TestBuildDeepPresentationRich:
    def test_all_sections_rendered(self, tmp_vault):
        """All sections are rendered when data is available."""
        sessions_dir = tmp_vault / "claude-sessions"
        insights_dir = tmp_vault / "claude-insights"
        _make_session_note(
            sessions_dir, "2026-04-10-proj-aaaa.md", "proj",
            body="## Summary\nSolo work.\n",
        )

        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]
        basenames_json = json.dumps(basenames)

        pipeline_path = str(tmp_vault / "pipeline.json")
        with open(pipeline_path, "w") as f:
            json.dump({
                "link_suggestions": [
                    {"note_a": "note-a", "note_b": "note-b", "shared_keywords": ["widget", "auth"]},
                ],
                "merge_suggestions": [
                    {"note_a": "insight-a", "note_b": "insight-b", "shared_keywords": ["pattern", "factory"]},
                ],
                "items": {
                    "total_raw": 3,
                    "groups": [
                        {
                            "project": "proj",
                            "representative": "Fix login handler",
                            "members": [
                                {"file": "a.md", "line": 10, "text": "Fix login handler"},
                                {"file": "b.md", "line": 15, "text": "Fix login handler", "confidence": "high"},
                            ],
                        }
                    ],
                    "group_count": 1,
                },
                "evidence": {},
            }, f)

        classifications_path = str(tmp_vault / "classifications.json")
        with open(classifications_path, "w") as f:
            json.dump({"some_key": "some_value"}, f)

        result = open_item_dedup.build_deep_presentation(
            pipeline_path=pipeline_path,
            classifications_path=classifications_path,
            basenames_json=basenames_json,
            vault_path=str(tmp_vault),
            sessions_folder="claude-sessions",
            insights_folder="claude-insights",
        )

        assert "## Open Item Consolidation" in result
        assert "Fix login handler" in result
        assert "## Suggested Links" in result
        assert "## Potential Insight Merges" in result
        assert "## Actions" in result

    def test_error_reading_pipeline(self, tmp_vault):
        """Graceful error when pipeline file is unreadable."""
        result = open_item_dedup.build_deep_presentation(
            pipeline_path="/nonexistent/path.json",
            classifications_path="/nonexistent/class.json",
            basenames_json="[]",
            vault_path=str(tmp_vault),
            sessions_folder="claude-sessions",
            insights_folder="claude-insights",
        )
        assert "Error" in result


class TestPipelineMultiProject:
    def test_multiple_projects(self, tmp_vault):
        """Pipeline handles multiple projects."""
        sessions_dir = tmp_vault / "claude-sessions"
        _make_session_note(
            sessions_dir, "2026-04-10-alpha-aaaa.md", "alpha",
            open_items=["Deploy alpha service"],
        )
        _make_session_note(
            sessions_dir, "2026-04-10-beta-bbbb.md", "beta",
            open_items=["Test beta integration"],
        )

        output_path = str(tmp_vault / "pipeline_out.json")
        basenames = [f.name for f in sessions_dir.iterdir() if f.suffix == ".md"]

        db_path = str(tmp_vault / "test.db")
        import vault_index
        vault_index.ensure_index(
            str(tmp_vault), ["claude-sessions", "claude-insights"], db_path=db_path
        )

        with patch.object(open_item_dedup, "_resolve_project_paths", return_value={}):
            result = open_item_dedup.deep_analysis_pipeline(
                basenames=basenames,
                projects_json='["alpha", "beta"]',
                output_path=output_path,
                vault_path=str(tmp_vault),
                sessions_folder="claude-sessions",
                insights_folder="claude-insights",
                db_path=db_path,
            )

        assert result.startswith("OK:")
        data = json.loads(open(output_path).read())
        assert data["items"]["total_raw"] == 2
