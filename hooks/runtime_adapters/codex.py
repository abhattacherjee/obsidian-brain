"""Codex context adapter; native payload identity takes precedence."""

from runtime_context import resolve_runtime_context
import os
from pathlib import Path


def selected_home(context=None):
    """Codex native storage never falls back to Claude configuration."""
    if context is not None:
        if context.host != 'codex':
            raise ValueError('Codex storage requires a Codex context')
        if getattr(context, 'native_home', None) is not None:
            return Path(context.native_home)
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()


def resolve_context(payload, overrides=None, client=None):
    if client not in {"codex-cli", "codex-desktop"}:
        from runtime_context import RuntimeContextError
        raise RuntimeContextError("client_unavailable", "Supply the current native Codex client explicitly.")
    return resolve_runtime_context("codex", client, payload, overrides or {})
