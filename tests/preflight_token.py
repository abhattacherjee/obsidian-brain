"""Where the vendored preflight pair keeps its token (harden-repo v1.4.1+).

``commit-preflight.sh`` writes, and ``require-preflight.py`` reads,
``${XDG_CACHE_HOME:-$HOME/.cache}/claude/preflight/harden-token-<hash>``.
XDG_CACHE_HOME counts only when absolute, and HOME must be absolute too.
``<hash>`` is the first 8 hex characters of md5 of the realpath of
``git rev-parse --path-format=absolute --git-common-dir``, so a worktree and
its main checkout share one token path. Before v1.4.1 the token was
``/tmp/.preflight-token-<hash>`` (#532).

conftest gives every test a tmp HOME and unsets XDG_CACHE_HOME, so these
paths never reach the developer's real cache.
"""

import hashlib
import os
import subprocess
from pathlib import Path


def token_dir(env=None):
    """The token directory by the hook's rule, or None when neither
    XDG_CACHE_HOME nor HOME is an absolute path."""
    env = os.environ if env is None else env
    root = env.get("XDG_CACHE_HOME", "")
    if not root.startswith("/"):
        home = env.get("HOME", "")
        if not home.startswith("/"):
            return None
        root = os.path.join(home, ".cache")
    return Path(root, "claude", "preflight")


def token_path(work, env=None, *, make_dir=True):
    """The token path for the repository at *work*.

    With ``make_dir`` the directory is created with mode 0700, as the hook
    requires of its last component, so a test can write a token there."""
    env = os.environ if env is None else env
    git_env = {k: v for k, v in env.items()
               if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))}
    common = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        env=git_env, check=True, capture_output=True, text=True,
        timeout=15).stdout.strip()
    digest = hashlib.md5(os.path.realpath(common).encode()).hexdigest()[:8]
    directory = token_dir(env)
    assert directory is not None, "no absolute XDG_CACHE_HOME or HOME"
    if make_dir:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    return directory / f"harden-token-{digest}"
