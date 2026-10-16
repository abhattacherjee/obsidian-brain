"""Curated wiki authorship stays separate from cited native source origins."""
import json
from pathlib import Path

import pytest
import wiki
from test_host_skill_operations import host, selected_host_context, context, invoke, note


@pytest.mark.parametrize('origin', ['claude', 'codex'])
def test_native_wiki_file_records_actor_without_rewriting_source_origin(context, origin):
    sources = [note(context, name + '.md', 'Historical evidence ' + name,
                    folder='claude-insights', kind='claude-insight', agent_provider=origin,
                    agent_session_id='historical-' + name) for name in ('a', 'b', 'c')]
    original = {path: path.read_bytes() for path in sources}
    payload = {'question':'How do we preserve historical evidence?',
               'body':'Preserve original source identity.\n\n### Sources\n- [[a]]\n- [[b]]\n- [[c]]\n',
               'sources':['a','b','c'], 'memory_sources':[], 'topics':['history'],
               'confidence':'high', 'filed_by':'user'}
    result = json.loads(invoke(context, 'vault-ask', 'wiki-file', {'data':payload})[1])
    metadata, body = wiki.read_page(Path(result['path']))
    assert metadata['author_host'] == context.host
    assert metadata['sources'] == ['[[a]]', '[[b]]', '[[c]]']
    assert 'agent_provider' not in metadata and 'agent_session_id' not in metadata
    assert 'Preserve original source identity.' in body
    for path, raw in original.items():
        assert path.read_bytes() == raw
        source_metadata, _ = wiki.read_page(path)
        assert source_metadata['agent_provider'] == origin
        assert source_metadata['agent_session_id'] == 'historical-' + path.stem
