"""Claude Code context adapter."""

from runtime_context import resolve_runtime_context
import os
from pathlib import Path


def selected_home(context=None):
    """Named Claude compatibility home; bound native selection takes precedence."""
    if context is not None:
        if context.host != 'claude':
            raise ValueError('Claude storage requires a Claude context')
        if getattr(context, 'native_home', None) is not None:
            return Path(context.native_home)
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').resolve()


def legacy_metrics_path():
    return selected_home() / 'obsidian-brain-summarizer-metrics.jsonl'


def legacy_index_path(use_override=True):
    """Keep the shipped Claude index default and explicit DB override seam."""
    return (os.environ.get('OBSIDIAN_BRAIN_DB') if use_override else None) or str(
        Path.home() / '.claude' / 'obsidian-brain-vault.db')


def legacy_workdir():
    return selected_home() / 'obsidian-brain'


def legacy_writer_context(vault, project, resource_root, index_path, state_path):
    """Explicit compatibility factory preserves caller-provided DB/state seams."""
    from runtime_context import RuntimeContext
    from types import MappingProxyType
    home = selected_home()
    return RuntimeContext('claude', 'cli', '', project, project, None, vault,
                          home / 'obsidian-brain-config.json', MappingProxyType({}),
                          resource_root, index_path, state_path, native_home=home)


def legacy_project_transcript_dir(project, context=None):
    """Claude's legacy slug heuristic chooses the shortest matching project."""
    base = selected_home(context) / 'projects'
    if not base.is_dir():
        return base / ('-NONEXISTENT-' + project)
    try:
        candidates = sorted((path for path in base.iterdir()
                             if path.is_dir() and path.name.endswith('-' + project)),
                            key=lambda path: len(path.name))
    except OSError:
        candidates = []
    return candidates[0] if candidates else base / ('-NONEXISTENT-' + project)


def legacy_reaper_watermark(project):
    return legacy_workdir() / ('reaper-watermark-' + project)


def resolve_context(payload, overrides=None):
    return resolve_runtime_context("claude", "claude-code", payload, overrides or {})


def legacy_private_directory():
    """Legacy default ~/.claude/obsidian-brain; bound callers use selected state."""
    return Path.home() / ".claude" / "obsidian-brain"


def legacy_config_path():
    """Legacy default ~/.claude/obsidian-brain-config.json."""
    return Path.home() / ".claude" / "obsidian-brain-config.json"


def legacy_foreign_host_markers():
    """Skip legacy Claude capture under a foreign native tool shell.

    Checked against codex-cli 0.155.1. CODEX_THREAD_ID detects unsandboxed
    native shells; older unsandboxed builds cannot be detected here.
    CODEX_HOME is user configuration and never evidence of the invoking host.
    """
    return ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")
