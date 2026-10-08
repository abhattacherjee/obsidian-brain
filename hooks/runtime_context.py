"""Resolve host identity and shared storage without guessing from another host."""

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
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


def path_is_within_physical_directory(path, directory):
    """Compare existing ancestor inodes so native case aliases remain contained."""
    candidate = Path(path).resolve()
    parent = Path(directory).resolve()
    try:
        identity = parent.stat()
    except FileNotFoundError:
        return candidate.is_relative_to(parent)
    for ancestor in (candidate, *candidate.parents):
        try:
            details = ancestor.stat()
        except FileNotFoundError:
            continue
        if (details.st_dev, details.st_ino) == (identity.st_dev, identity.st_ino):
            return True
    return False


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
    native_home: Optional[Path] = None
    user_home: Optional[Path] = None
    invocation_cwd: Optional[Path] = None
    coordination_root: Optional[Path] = None

    def __post_init__(self):
        if self.user_home is None:
            object.__setattr__(self, 'user_home', Path.home().resolve())
        if self.coordination_root is None:
            import pwd
            configured = os.environ.get('XDG_STATE_HOME')
            if configured:
                root = Path(configured)
                if not root.is_absolute():
                    raise RuntimeContextError('state_invalid', 'XDG_STATE_HOME must be absolute.')
            else:
                root = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local' / 'state'
            root = root.resolve()
            if path_is_within_physical_directory(root, self.vault_path):
                root = Path('/var/tmp') / ('obsidian-brain-state-' + str(os.getuid()))
                root = root.resolve()
            if path_is_within_physical_directory(root, self.vault_path):
                raise RuntimeContextError('state_invalid', 'Coordination state must be outside the vault.')
            object.__setattr__(self, 'coordination_root', root)


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
                            overrides: Mapping[str, object], *,
                            require_payload_session: bool = False) -> RuntimeContext:
    clients = {"claude": {"claude-code", "cli"}, "codex": {"codex-cli", "codex-desktop", "cli"}}
    if host not in clients:
        raise RuntimeContextError("host_unknown", "Select a supported host explicitly.")
    if client not in clients[host]:
        raise RuntimeContextError("client_mismatch", "The client does not belong to the selected host.")
    home = Path.home()
    native_home = _path(os.environ.get("CODEX_HOME", home / ".codex") if host == "codex"
                        else os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    payload_sid = payload.get("session_id", payload.get("sessionId"))
    if require_payload_session:
        if not isinstance(payload_sid, str) or not payload_sid.strip():
            raise RuntimeContextError("session_missing", "The native hook payload did not supply a session ID.")
        sid = payload_sid
    else:
        sid = overrides.get("session_id", payload_sid)
    if sid is None:
        sid = os.environ.get("CODEX_THREAD_ID" if host == "codex" else "CLAUDE_CODE_SESSION_ID", "")
    if not isinstance(sid, str) or (not sid.strip() and client != "cli"):
        raise RuntimeContextError("session_missing", "The selected host did not supply a native session ID.")
    cwd = overrides.get("cwd", payload.get("cwd"))
    if cwd is None:
        cwd = os.getcwd()
    missing_project = False
    try:
        worktree, project = _project(cwd)
    except RuntimeContextError as exc:
        if exc.code != "project_missing":
            raise
        worktree = project = _path(cwd)
        missing_project = True
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
        transcript_roots = ([native_home / "sessions", native_home / "archived_sessions"]
                            if host == "codex" else [native_home / "projects"])
        if not any(transcript.is_relative_to(root.resolve()) for root in transcript_roots):
            raise RuntimeContextError("transcript_outside_host", "The transcript is outside the selected host's storage.")
    context = RuntimeContext(host, client, sid, project, worktree, transcript, vault, config_path,
                             MappingProxyType(copy.deepcopy(config)), resources, index, state, native_home, home.resolve())
    context = replace(context, invocation_cwd=_path(cwd))
    return _registered_missing_project(context) if missing_project else context


def _registered_missing_project(context):
    """Recover only an earlier verified registration for this exact native actor."""
    import sqlite3
    import stat
    from note_transactions import coordination_location
    database = coordination_location(context) / "state.sqlite3"
    connection = None
    try:
        metadata = database.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("Untrusted registry file")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0)  # noqa: vault-db-connect — exact read-only registered identity recovery
        identity = connection.execute("SELECT vault FROM identity LIMIT 1").fetchone()
        if not identity or identity[0] != str(context.vault_path.resolve()):
            raise ValueError("Registry vault mismatch")
        row = connection.execute("SELECT descriptor FROM source_sessions WHERE scope=?", (context.session_key,)).fetchone()
        descriptor = json.loads(row[0]) if row else {}
        if (descriptor.get("host") != context.host or descriptor.get("native_session_id") != context.native_session_id
                or descriptor.get("invocation_cwd", descriptor.get("worktree")) != str(context.invocation_cwd or context.worktree)
                or descriptor.get("config_path") != str(context.config_path)
                or descriptor.get("state_path") != str(context.state_path)):
            raise ValueError("No matching registration")
        registered_worktree = Path(descriptor["worktree"])
        original_cwd = Path(descriptor.get("invocation_cwd", descriptor["worktree"]))
        if (not registered_worktree.is_absolute() or registered_worktree.resolve() != registered_worktree
                or not original_cwd.is_relative_to(registered_worktree)):
            raise ValueError("Registered working directory escaped its worktree")
        root = Path(descriptor["canonical_project_root"])
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("Registered project changed identity")
        transcript = context.transcript_path
        if transcript is None and descriptor.get("transcript_path"):
            transcript = _path(descriptor["transcript_path"])
        if transcript is not None:
            roots = ([context.native_home / "projects"] if context.host == "claude" else
                     [context.native_home / "sessions", context.native_home / "archived_sessions"])
            if not any(transcript.is_relative_to(path.resolve()) for path in roots):
                raise ValueError("Registered transcript escaped native storage")
        return replace(context, canonical_project_root=root, worktree=registered_worktree, transcript_path=transcript)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        raise RuntimeContextError("project_missing", "The removed working directory has no verified registration.") from exc
    finally:
        if connection is not None:
            connection.close()


def historical_source_roots(source_host, source_root=None):
    """Select read-only historical origins without changing the invoking host."""
    if source_host not in {'claude', 'codex'}:
        raise RuntimeContextError('host_unknown', 'Select the historical source host explicitly.')
    def checked_directory(value, require_exists=False, trusted_home=False):
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip() or '\0' in str(value):
            raise RuntimeContextError('path_invalid', 'Historical roots must be nonempty path strings.')
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeContextError('path_invalid', 'Historical roots must be absolute directories.')
        if not trusted_home and any(part.is_symlink() for part in (path, *path.parents)):
            raise RuntimeContextError('path_invalid', 'Historical roots cannot contain symbolic links.')
        if (require_exists or path.exists()) and not path.is_dir():
            raise RuntimeContextError('path_invalid', 'Historical root must be a directory.')
        return path.resolve()
    if source_root is not None:
        return (checked_directory(source_root, require_exists=True),)
    environment, name = ('CLAUDE_CONFIG_DIR', '.claude') if source_host == 'claude' else ('CODEX_HOME', '.codex')
    # The configured home is an explicit trust root and may be a dotfiles
    # symlink. Resolve it once; links below that root remain forbidden.
    native_home = checked_directory(os.environ.get(environment, Path.home() / name), trusted_home=True)
    subdirectories = ('projects',) if source_host == 'claude' else ('sessions', 'archived_sessions')
    return tuple(checked_directory(native_home / name) for name in subdirectories)


def native_memory_projects_root(context=None):
    """Return selected Claude memory storage, without consulting another host."""
    context = context if context is not None else current_runtime_context()
    if context is None or context.host != 'claude':
        return None
    if context.native_home is None:
        return historical_source_roots('claude')[0]
    home = Path(context.native_home)
    if not home.is_absolute():
        raise RuntimeContextError('path_invalid', 'Native memory home must be an absolute directory.')
    home = home.resolve()
    if home.exists() and not home.is_dir():
        raise RuntimeContextError('path_invalid', 'Native memory home must be a directory.')
    projects = home / 'projects'
    if projects.is_symlink():
        raise RuntimeContextError('path_invalid', 'Native memory storage cannot be a symbolic link.')
    return projects.resolve()


def selected_native_environment(context, base_environment):
    """Copy caller environment and bind only the invoking host's frozen home."""
    if context.host not in {'claude', 'codex'} or context.native_home is None:
        raise ValueError('A selected native host and frozen native home are required.')
    home = Path(context.native_home)
    if not home.is_absolute() or home != home.resolve() or home.is_symlink():
        raise ValueError('The frozen native home must be canonical and absolute.')
    environment = dict(base_environment)
    environment['CLAUDE_CONFIG_DIR' if context.host == 'claude' else 'CODEX_HOME'] = str(home)
    return environment
