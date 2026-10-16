"""Reject ambiguous operation inputs before resolving identity or touching state."""

import pytest

from brain_cli import read_payload
from runtime_context import RuntimeContextError


@pytest.mark.parametrize("raw", [
    '{"operation_id":"one","operation_id":"two"}',
    '{"inputs":{"path":"one","path":"two"}}',
    '{"timeout":NaN}', '{"timeout":Infinity}', '{"timeout":-Infinity}',
])
def test_operation_payload_requires_unambiguous_finite_json(raw):
    with pytest.raises(RuntimeContextError) as exc:
        read_payload(raw)
    assert exc.value.code == "input_invalid"


def test_payload_preserves_repeated_values_in_distinct_objects():
    assert read_payload('{"left":{"path":"one"},"right":{"path":"two"}}') == {
        "left": {"path": "one"}, "right": {"path": "two"}}
