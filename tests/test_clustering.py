import os, sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))
import clustering


def _v(**kw):
    return dict(kw)


def test_three_similar_notes_form_one_cluster():
    items = [
        ("a", _v(tfidf=1.0, theme=0.9)),
        ("b", _v(tfidf=1.0, theme=0.8)),
        ("c", _v(tfidf=0.9, theme=1.0)),
        ("z", _v(unrelated=1.0)),  # shares no terms -> sim 0
    ]
    clusters = clustering.cluster_vectors(items, threshold=0.5, min_cluster_size=3)
    assert clusters == [["a", "b", "c"]]  # z dropped (cluster of 1 < min_size)


def test_min_cluster_size_drops_small_groups():
    items = [("a", _v(x=1.0)), ("b", _v(x=1.0))]  # pair, but < 3
    assert clustering.cluster_vectors(items, threshold=0.5, min_cluster_size=3) == []


def test_transitive_single_linkage_chains():
    # a~b (sim>=t), b~c (sim>=t), but a~c may be < t: single-linkage still groups all three.
    items = [
        ("a", _v(p=1.0, q=0.6)),
        ("b", _v(q=1.0, r=0.6)),
        ("c", _v(r=1.0, s=0.6)),
    ]
    clusters = clustering.cluster_vectors(items, threshold=0.4, min_cluster_size=3)
    assert clusters == [["a", "b", "c"]]


def test_deterministic_ordering_largest_first_then_lex():
    items = [
        ("m", _v(big=1.0)), ("n", _v(big=1.0)), ("o", _v(big=1.0)), ("p", _v(big=1.0)),
        ("d", _v(sml=1.0)), ("e", _v(sml=1.0)), ("f", _v(sml=1.0)),
    ]
    clusters = clustering.cluster_vectors(items, threshold=0.5, min_cluster_size=3)
    assert clusters == [["m", "n", "o", "p"], ["d", "e", "f"]]  # size desc, members sorted
    # input order must not matter:
    import random
    shuffled = list(items)
    random.Random(1).shuffle(shuffled)
    assert clustering.cluster_vectors(shuffled, threshold=0.5, min_cluster_size=3) == clusters


def test_empty_and_singleton_inputs():
    assert clustering.cluster_vectors([], threshold=0.5, min_cluster_size=3) == []
    assert clustering.cluster_vectors([("a", {"x": 1.0})], threshold=0.5, min_cluster_size=3) == []


@pytest.mark.skipif(
    not __import__("obsidian_utils").check_optional_deps(("numpy", "scipy"))["scipy"],
    reason="scipy not installed",
)
def test_fast_path_matches_stdlib_fallback():
    # --- Structural fixture: 3 term-disjoint groups + 2 singletons ---
    # Intra-group cosine ≈ 1.0, inter-group cosine = 0 (disjoint vocabularies).
    # Guarantees >=2 clusters so we prove both paths return non-trivial identical structure.
    group_a = [(f"a{i}", {"a": 1.0, "b": 0.9}) for i in range(5)]  # 5 notes, terms a+b
    group_b = [(f"b{i}", {"c": 1.0, "d": 0.9}) for i in range(4)]  # 4 notes, terms c+d
    group_c = [(f"c{i}", {"e": 1.0, "f": 0.9}) for i in range(3)]  # 3 notes, terms e+f
    singletons = [("s0", {"g": 1.0}), ("s1", {"h": 1.0})]           # 2 singletons, unique terms
    items = group_a + group_b + group_c + singletons
    fast = clustering.cluster_vectors(items, threshold=0.5, min_cluster_size=3, _force_stdlib=False)
    slow = clustering.cluster_vectors(items, threshold=0.5, min_cluster_size=3, _force_stdlib=True)
    assert fast == slow
    assert len(fast) >= 2  # at least 2 clusters found (not a single-blob collapse)

    # --- Boundary-sensitive fixture: 40 random-weight notes, many pairs near threshold ---
    # Uses random weights so cosine similarities are scattered around 0.3; a >= vs >
    # or threshold-drift mutation in _components_fast causes parity to break.
    import random
    rng = random.Random(7)
    all_terms = [f"t{i}" for i in range(12)]
    rand_items = []
    for idx in range(40):
        terms = rng.sample(all_terms, 4)
        vec = {t: round(rng.random(), 3) for t in terms}
        rand_items.append((f"r{idx}", vec))
    fast_rand = clustering.cluster_vectors(rand_items, threshold=0.3, min_cluster_size=3, _force_stdlib=False)
    slow_rand = clustering.cluster_vectors(rand_items, threshold=0.3, min_cluster_size=3, _force_stdlib=True)
    assert fast_rand == slow_rand

    # Analytic exact-threshold chain: each adjacent pair has cosine EXACTLY 0.5
    # (dot=2, both norms=2 -> 2/(2*2)=0.5), so `>=` links them but `>` would not.
    # Single-linkage chains n1-n2-n3 into one cluster of 3 under `>=`; a `>`-mutated
    # fast path yields zero edges -> 3 dropped singletons -> empty, breaking parity.
    exact = [
        ("n1", {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0}),
        ("n2", {"a": 1.0, "b": 1.0, "e": 1.0, "f": 1.0}),  # shares a,b with n1 -> cos 0.5
        ("n3", {"e": 1.0, "f": 1.0, "g": 1.0, "h": 1.0}),  # shares e,f with n2 -> cos 0.5
    ]
    ex_fast = clustering.cluster_vectors(exact, threshold=0.5, min_cluster_size=3, _force_stdlib=False)
    ex_slow = clustering.cluster_vectors(exact, threshold=0.5, min_cluster_size=3, _force_stdlib=True)
    assert ex_fast == ex_slow
    assert ex_fast == [["n1", "n2", "n3"]]  # `>=` at exactly-0.5 links the chain
