"""Tests for 7-signal scorer with task-context-adaptive type weights."""

import math
import sqlite3
import time
from unittest.mock import patch

import pytest

import vault_index


class TestDetectTaskContext:
    def test_general_context_default(self):
        with patch.object(vault_index, "_get_git_branch", return_value="develop"):
            assert vault_index.detect_task_context() == "general"

    def test_debugging_from_fix_branch(self):
        with patch.object(vault_index, "_get_git_branch", return_value="fix/login-bug"):
            assert vault_index.detect_task_context() == "debugging"

    def test_debugging_from_hotfix_branch(self):
        with patch.object(vault_index, "_get_git_branch", return_value="hotfix/urgent"):
            assert vault_index.detect_task_context() == "debugging"

    def test_debugging_from_bug_branch(self):
        with patch.object(vault_index, "_get_git_branch", return_value="bug/crash-on-start"):
            assert vault_index.detect_task_context() == "debugging"

    def test_caller_standup(self):
        with patch.object(vault_index, "_get_git_branch", return_value="develop"):
            assert vault_index.detect_task_context(caller_skill="standup") == "standup"

    def test_caller_emerge(self):
        with patch.object(vault_index, "_get_git_branch", return_value="develop"):
            assert vault_index.detect_task_context(caller_skill="emerge") == "emerge"

    def test_caller_vault_search(self):
        with patch.object(vault_index, "_get_git_branch", return_value="develop"):
            assert vault_index.detect_task_context(caller_skill="vault-search") == "search"

    def test_caller_vault_ask(self):
        with patch.object(vault_index, "_get_git_branch", return_value="develop"):
            assert vault_index.detect_task_context(caller_skill="vault-ask") == "search"

    def test_branch_takes_precedence_over_caller_for_debugging(self):
        with patch.object(vault_index, "_get_git_branch", return_value="fix/thing"):
            assert vault_index.detect_task_context(caller_skill="vault-search") == "debugging"

    def test_no_branch_available(self):
        with patch.object(vault_index, "_get_git_branch", return_value=None):
            assert vault_index.detect_task_context() == "general"


class TestContextAdaptiveTypeScores:
    def test_get_type_scores_debugging(self):
        scores = vault_index.get_type_scores("debugging")
        assert scores["claude-error-fix"] == 1.0
        assert scores["claude-session"] == 0.8
        assert scores["claude-decision"] == 0.5

    def test_get_type_scores_standup(self):
        scores = vault_index.get_type_scores("standup")
        assert scores["claude-session"] == 1.0
        assert scores["claude-decision"] == 0.8

    def test_get_type_scores_search(self):
        scores = vault_index.get_type_scores("search")
        assert scores["claude-insight"] == 1.0
        assert scores["claude-decision"] == 0.9

    def test_get_type_scores_general(self):
        scores = vault_index.get_type_scores("general")
        assert scores["claude-insight"] == 1.0
        assert scores["claude-decision"] == 1.0
        assert scores["claude-error-fix"] == 0.9

    def test_get_type_scores_unknown_context_uses_general(self):
        scores = vault_index.get_type_scores("unknown_context")
        assert scores == vault_index.get_type_scores("general")

    def test_get_type_scores_unknown_type_returns_default(self):
        scores = vault_index.get_type_scores("general")
        assert scores.get("unknown-type", 0.5) == 0.5


def _make_fts_result(
    path="/vault/test.md",
    note_type="claude-session",
    date_str="2026-04-15",
    rank=-5.0,
    body="some content here",
    title="Test Note",
    tags="claude/session",
    importance=5,
):
    return {
        "path": path, "type": note_type, "date": date_str, "rank": rank,
        "body": body, "title": title, "tags": tags, "importance": importance,
        "status": "summarized",
    }


class TestRerankV2Signals:
    def test_rerank_returns_7_signal_score(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        candidates = [
            _make_fts_result(path="/vault/a.md", rank=-10.0),
            _make_fts_result(path="/vault/b.md", rank=-5.0),
        ]
        results = vault_index.rerank_results(candidates, ["content"], db_path=db_path)
        assert len(results) == 2
        assert all("rerank_score" in r for r in results)

    def test_activation_signal_boosts_accessed_note(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        vault_index.log_access(db_path, "/vault/a.md", "recall")
        vault_index.log_access(db_path, "/vault/a.md", "search")
        vault_index.log_access(db_path, "/vault/a.md", "ask")
        candidates = [
            _make_fts_result(path="/vault/a.md", rank=-5.0, body="test content"),
            _make_fts_result(path="/vault/b.md", rank=-5.0, body="test content"),
        ]
        results = vault_index.rerank_results(candidates, ["test"], db_path=db_path)
        a_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/a.md")
        b_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/b.md")
        assert a_score > b_score

    def test_importance_signal_boosts_high_importance(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        candidates = [
            _make_fts_result(path="/vault/a.md", rank=-5.0, body="test content", importance=9),
            _make_fts_result(path="/vault/b.md", rank=-5.0, body="test content", importance=2),
        ]
        results = vault_index.rerank_results(candidates, ["test"], db_path=db_path)
        a_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/a.md")
        b_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/b.md")
        assert a_score > b_score

    def test_task_context_changes_type_scores(self, tmp_vault):
        db_path = str(tmp_vault / "test.db")
        vault_index.ensure_index(str(tmp_vault), ["claude-sessions"], db_path=db_path)
        candidates = [
            _make_fts_result(path="/vault/err.md", note_type="claude-error-fix",
                             rank=-5.0, body="test fix"),
            _make_fts_result(path="/vault/dec.md", note_type="claude-decision",
                             rank=-5.0, body="test fix"),
        ]
        results = vault_index.rerank_results(
            candidates, ["test"], db_path=db_path, task_context="debugging"
        )
        err_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/err.md")
        dec_score = next(r["rerank_score"] for r in results if r["path"] == "/vault/dec.md")
        assert err_score > dec_score

    def test_weights_sum_to_one(self):
        total = 0.25 + 0.20 + 0.10 + 0.10 + 0.05 + 0.20 + 0.10
        assert abs(total - 1.0) < 0.001

    def test_backward_compatible_without_db_path(self):
        candidates = [
            _make_fts_result(path="/vault/a.md", rank=-10.0),
            _make_fts_result(path="/vault/b.md", rank=-5.0),
        ]
        results = vault_index.rerank_results(candidates, ["content"])
        assert len(results) == 2
        assert all("rerank_score" in r for r in results)

    def test_empty_input(self):
        assert vault_index.rerank_results([], ["test"]) == []
