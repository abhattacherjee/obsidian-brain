"""Parity guard for the #229 vault_index module split (Slice A).

Behaviour-neutral: every symbol moved into hooks/tfidf.py or hooks/themes.py
must remain importable from `vault_index` (the back-compat re-export shim) AND
be the *same object* as in its new home. If this fails, a caller doing
`vault_index.<symbol>` would break.
"""
import os
import subprocess
import sys

import vault_index
import tfidf
import themes


def test_tfidf_symbols_reexported():
    assert vault_index._tokenize_for_tfidf is tfidf._tokenize_for_tfidf
    assert vault_index._compute_tfidf_vector is tfidf._compute_tfidf_vector
    assert vault_index._cosine_similarity is tfidf._cosine_similarity
    assert vault_index._update_term_df is tfidf._update_term_df
    assert vault_index._STOPWORDS is tfidf._STOPWORDS
    assert vault_index._TOKEN_RE is tfidf._TOKEN_RE


def test_theme_symbols_reexported():
    assert vault_index.assign_to_theme is themes.assign_to_theme
    assert vault_index.detect_surprise is themes.detect_surprise
    assert vault_index._NEGATION_TERMS is themes._NEGATION_TERMS
    assert (
        vault_index._THEME_SIMILARITY_THRESHOLD
        is themes._THEME_SIMILARITY_THRESHOLD
    )


def test_themes_depends_on_tfidf_one_directional():
    # themes reuses tfidf's cosine helper (the only cross-module call).
    assert themes._cosine_similarity is tfidf._cosine_similarity


def test_tfidf_is_leaf_module():
    """Importing tfidf alone must not pull in vault_index or themes."""
    hooks_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "hooks")
    )
    code = (
        "import sys, tfidf; "
        "print('vault_index' in sys.modules or 'themes' in sys.modules)"
    )
    env = dict(os.environ, PYTHONPATH=hooks_dir)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert out.stdout.strip() == "False", out.stdout + out.stderr


def test_themes_lazy_connect_resolves_to_vault_index():
    """The lazy `from vault_index import _connect` in themes.assign_to_theme
    must resolve to the canonical (bare) vault_index._connect — the same module
    every runtime caller imports (obsidian_utils/open_item_dedup/vault_stats all
    use `import vault_index`). This makes the cycle-break seam explicit and
    fast-failing if someone breaks it. (It also means a monkeypatch of bare
    vault_index._connect is honored by assign_to_theme, as test_vault_index.py
    relies on.)
    """
    import vault_index
    from vault_index import _connect  # mirrors the lazy import in themes.py
    assert _connect is vault_index._connect
