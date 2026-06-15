"""Pure agglomerative clustering over sparse TF-IDF vectors.

Single-linkage clustering = connected components of the graph where an edge
exists between two notes iff cosine_similarity >= threshold. Deterministic and
order-invariant. numpy + scipy.sparse.csgraph fast path when importable; a
pure-stdlib union-find fallback otherwise. Both paths return identical clusters
by construction — they only differ in how the adjacency is computed. No DB, no
LLM, no I/O. Depends only on the stdlib plus the optional numpy/scipy fast path.
"""

from __future__ import annotations

from tfidf import _cosine_similarity


def _components_stdlib(items: list[tuple[str, dict[str, float]]], threshold: float) -> dict[int, list[int]]:
    """O(n^2) pairwise cosine + union-find. Returns dict root_index -> [indices]."""
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    vecs = [v for _, v in items]
    for i in range(n):
        for j in range(i + 1, n):
            if _cosine_similarity(vecs[i], vecs[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return groups


def _components_fast(items: list[tuple[str, dict[str, float]]], threshold: float) -> dict[int, list[int]]:
    """numpy/scipy fast path: CSR cosine matrix -> thresholded sparse graph ->
    connected_components. Stays sparse throughout — never materialises the N×N
    dense matrix. Returns dict label -> [indices]."""
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    vocab = {}
    for _, vec in items:
        for term in vec:
            vocab.setdefault(term, len(vocab))
    n, m = len(items), len(vocab)
    rows, cols, data = [], [], []
    for i, (_, vec) in enumerate(items):
        for term, w in vec.items():
            rows.append(i)
            cols.append(vocab[term])
            data.append(w)
    X = csr_matrix((data, (rows, cols)), shape=(n, max(m, 1)), dtype=float)
    norms = np.sqrt(X.multiply(X).sum(axis=1)).A1
    norms[norms == 0] = 1.0
    inv = 1.0 / norms
    Xn = X.multiply(inv[:, None]).tocsr()
    sim = (Xn @ Xn.T)              # sparse CSR; implicit zeros stay implicit
    adj = (sim >= threshold)       # threshold>0 keeps implicit zeros out -> stays sparse
    adj.setdiag(False)
    adj.eliminate_zeros()
    _, labels = connected_components(adj, directed=False)
    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(i)
    return groups


def cluster_vectors(items: list[tuple[str, dict[str, float]]], threshold: float = 0.5, min_cluster_size: int = 3, _force_stdlib: bool | None = None) -> list[list[str]]:
    """Cluster ``items`` (list of ``(note_path, sparse_vec)``) into single-linkage
    groups. Returns a list of clusters (each a list of note_paths), keeping only
    clusters with >= ``min_cluster_size`` members. Deterministic order: largest
    cluster first, ties broken by the lexicographically smallest member path;
    members within a cluster are sorted ascending.

    ``_force_stdlib`` is a test seam: None -> auto-detect, True -> stdlib, False ->
    require the fast path (raises if numpy/scipy missing).
    """
    if len(items) < min_cluster_size:
        return []

    use_fast = _force_stdlib is False
    if _force_stdlib is None:
        from obsidian_utils import check_optional_deps
        deps = check_optional_deps(("numpy", "scipy"))
        use_fast = deps["numpy"] and deps["scipy"]

    groups = _components_fast(items, threshold) if use_fast else _components_stdlib(items, threshold)

    clusters = []
    for idxs in groups.values():
        if len(idxs) >= min_cluster_size:
            clusters.append(sorted(items[i][0] for i in idxs))
    clusters.sort(key=lambda c: (-len(c), c[0]))
    return clusters
