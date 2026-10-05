"""Codex context adapter; native payload identity takes precedence."""

from runtime_context import resolve_runtime_context


def resolve_context(payload, overrides=None, client="codex-cli"):
    return resolve_runtime_context("codex", client, payload, overrides or {})
