"""Claude Code context adapter."""

from runtime_context import resolve_runtime_context


def resolve_context(payload, overrides=None):
    return resolve_runtime_context("claude", "claude-code", payload, overrides or {})
