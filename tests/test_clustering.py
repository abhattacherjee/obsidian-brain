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
    import random
    rng = random.Random(7)
    terms = [f"t{i}" for i in range(12)]
    items = []
    for n in range(40):
        vec = {t: round(rng.random(), 3) for t in rng.sample(terms, 4)}
        items.append((f"note{n:02d}", vec))
    fast = clustering.cluster_vectors(items, threshold=0.3, min_cluster_size=3, _force_stdlib=False)
    slow = clustering.cluster_vectors(items, threshold=0.3, min_cluster_size=3, _force_stdlib=True)
    assert fast == slow
