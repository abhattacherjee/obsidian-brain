"""Resolve host identity and shared storage without guessing from another host."""

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

_ACTIVE_CONTEXT = ContextVar("obsidian_brain_runtime_context", default=None)


def current_runtime_context():
    """Return a context supplied by a native entry point, without host detection."""
    return _ACTIVE_CONTEXT.get()


@contextmanager
def using_runtime_context(context):
    """Bind a context only for this call, including compatible legacy imports."""
    token = _ACTIVE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _ACTIVE_CONTEXT.reset(token)


class RuntimeContextError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeContext:
    host: str
    client: str
    native_session_id: str
    canonical_project_root: Path
    worktree: Path
    transcript_path: Optional[Path]
    vault_path: Path
    config_path: Path
    config: Mapping[str, object]
    resource_root: Path
    index_path: Path
    state_path: Path

    @property
    def session_key(self) -> str:
        return hashlib.sha256((self.host + "\0" + self.native_session_id).encode()).hexdigest()


def _path(value, base: Optional[Path] = None) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip() or "\0" in str(value):
        raise RuntimeContextError("path_invalid", "Runtime paths must be nonempty path strings.")
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def _project(cwd):
    worktree = _path(cwd)
    if not worktree.is_dir():
        raise RuntimeContextError("project_missing", "The native working directory no longer exists.")
    for directory in (worktree, *worktree.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return directory, directory
        if marker.is_file():
            try:
                value = marker.read_text().strip()
                if not value.startswith("gitdir: "):
                    raise ValueError("Invalid gitdir marker")
                gitdir = _path(value[8:], directory)
                common_file = gitdir / "commondir"
                common = _path(common_file.read_text().strip(), gitdir) if common_file.exists() else gitdir
                if not common.is_dir():
                    raise ValueError("Missing Git common directory")
                return directory, common.parent if common_file.exists() else directory
            except (OSError, ValueError) as exc:
                raise RuntimeContextError("project_invalid", "Cannot resolve the worktree's Git directory.") from exc
    return worktree, worktree


def resolve_runtime_context(host: str, client: str, payload: Mapping[str, object],
                            overrides: Mapping[str, object]) -> RuntimeContext:
    clients = {"claude": {"claude-code", "cli"}, "codex": {"codex-cli", "codex-desktop", "cli"}}
    if host not in clients:
        raise RuntimeContextError("host_unknown", "Select a supported host explicitly.")
    if client not in clients[host]:
        raise RuntimeContextError("client_mismatch", "The client does not belong to the selected host.")
    home = Path.home()
    native_home = _path(os.environ.get("CODEX_HOME", home / ".codex") if host == "codex"
                        else os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    sid = overrides.get("session_id", payload.get("session_id", payload.get("sessionId")))
    if sid is None:
        sid = os.environ.get("CODEX_THREAD_ID" if host == "codex" else "CLAUDE_CODE_SESSION_ID", "")
    if not isinstance(sid, str) or (not sid.strip() and client != "cli"):
        raise RuntimeContextError("session_missing", "The selected host did not supply a native session ID.")
    worktree, project = _project(overrides.get("cwd", payload.get("cwd", os.getcwd())))
    config_path = _path(overrides.get("config_path") or os.environ.get("OBSIDIAN_BRAIN_CONFIG")
                        or native_home / "obsidian-brain-config.json")
    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError("Configuration must be an object")
    except (OSError, ValueError) as exc:
        raise RuntimeContextError("config_invalid", "Cannot read the selected runtime configuration.") from exc
    vault_value = overrides.get("vault_path") or config.get("vault_path")
    if not vault_value:
        raise RuntimeContextError("vault_missing", "Configure a vault path for the selected host.")
    vault = _path(vault_value, config_path.parent)
    legacy_index = home / ".claude" / "obsidian-brain-vault.db"
    index_value = overrides.get("index_path") or os.environ.get("OBSIDIAN_BRAIN_DB") or config.get("index_path")
    if index_value:
        index = _path(index_value, config_path.parent)
    elif legacy_index.exists():
        index = legacy_index.resolve()
    else:
        data_home = _path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        vault_key = hashlib.sha256(str(vault).encode()).hexdigest()
        index = data_home / "obsidian-brain" / "vaults" / vault_key / "index.sqlite3"
    state = _path(overrides.get("state_path") or os.environ.get("OBSIDIAN_BRAIN_STATE_DIR")
                  or native_home / "obsidian-brain" / ("state" if host == "codex" else ""))
    resource_value = overrides.get("resource_root") or os.environ.get("PLUGIN_ROOT")
    if not resource_value and host == "claude":
        resource_value = os.environ.get("CLAUDE_PLUGIN_ROOT")
    resources = _path(resource_value or Path(__file__).resolve().parent.parent)
    descriptor = ".codex-plugin" if host == "codex" else ".claude-plugin"
    if not (resources / "hooks").is_dir() or not (resources / descriptor / "plugin.json").is_file():
        raise RuntimeContextError("resources_missing", "The selected plugin installation is incomplete.")
    transcript_value = overrides.get("transcript_path", payload.get("transcript_path"))
    transcript = _path(transcript_value) if transcript_value else None
    if transcript is not None:
        transcript_root = native_home / ("sessions" if host == "codex" else "projects")
        try:
            transcript.relative_to(transcript_root.resolve())
        except ValueError as exc:
            raise RuntimeContextError("transcript_outside_host", "The transcript is outside the selected host's storage.") from exc
    return RuntimeContext(host, client, sid, project, worktree, transcript, vault, config_path,
                          MappingProxyType(copy.deepcopy(config)), resources, index, state)
