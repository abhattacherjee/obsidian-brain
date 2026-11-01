"""Scratch test for #485 forced-failure run. Never merge."""


def test_uses_a_python_310_api():
    # int.bit_count was added in 3.10, so this fails only on 3.9.
    assert (7).bit_count() == 3
