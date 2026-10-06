"""Inline classifier retries cannot publish stale or partial output."""
import json
from pathlib import Path
import pytest
import check_items_cli as cli
from check_items_test_helpers import native_ai_context, ai_response, verdicts, private_output


def group(number):
    return {'group_id':f'g{number}','project':'p','representative':f'Fix bug #{number}',
            'instances':[]}


@pytest.mark.parametrize('existing', ['', '{not json', '\n \n', '[{"group_id":"old"}]'])
def test_existing_output_is_never_a_model_response(tmp_path,monkeypatch,existing):
    output=private_output('out.json');output.write_text(existing)
    monkeypatch.setattr(cli,'_request_ai',lambda *args:ai_response(None))
    rc,data=cli._dispatch_classifier_chunk([group(1)],{},'haiku',str(output))
    assert rc==4 and data==[]
    assert output.read_text()==existing


def test_inline_valid_result_does_not_read_or_publish_chunk_output(tmp_path,monkeypatch):
    output=private_output('out.json');output.write_text('{old malformed file')
    monkeypatch.setattr(cli,'_request_ai',lambda *args:ai_response(verdicts([group(1)])))
    rc,data=cli._dispatch_classifier_chunk([group(1)],{},'haiku',str(output))
    assert rc==0 and data[0]['group_id']=='g1'
    assert output.read_text()=='{old malformed file'


def scripted_dispatch(monkeypatch,script):
    calls=[]
    def dispatch(groups,evidence,model,output_path,chunk_label=''):
        ids=tuple(g['group_id'] for g in groups);calls.append(ids)
        values=script[ids]
        return values.pop(0) if len(values)>1 else values[0]
    monkeypatch.setattr(cli,'_dispatch_classifier_chunk',dispatch)
    monkeypatch.setattr(cli,'CLASSIFIER_CHUNK_SIZE',2)
    monkeypatch.setenv('CHECK_ITEMS_PREFILTER','off')
    return calls


def test_first_exhausted_chunk_stops_and_preserves_previous_output(tmp_path,monkeypatch):
    groups=[group(n) for n in range(1,7)]
    calls=scripted_dispatch(monkeypatch,{('g1','g2'):[(3,[]),(3,[])]})
    output=private_output('out.json');output.write_text('previous')
    assert cli.run_classifier(json.dumps({'groups':groups,'evidence':{}}),str(output))==3
    assert calls==[('g1','g2'),('g1','g2')]
    assert output.read_text()=='previous'


def test_completed_chunk_is_not_published_when_later_chunk_fails(tmp_path,monkeypatch):
    groups=[group(n) for n in range(1,5)]
    calls=scripted_dispatch(monkeypatch,{
        ('g1','g2'):[(0,verdicts(groups[:2]))],('g3','g4'):[(4,[]),(4,[])]})
    output=private_output('out.json');output.write_text('previous')
    assert cli.run_classifier(json.dumps({'groups':groups,'evidence':{}}),str(output))==4
    assert calls==[('g1','g2'),('g3','g4'),('g3','g4')]
    assert output.read_text()=='previous'


def test_retry_success_publishes_all_requested_groups_in_order(tmp_path,monkeypatch):
    groups=[group(n) for n in range(1,5)]
    calls=scripted_dispatch(monkeypatch,{
        ('g1','g2'):[(0,verdicts(list(reversed(groups[:2]))))],
        ('g3','g4'):[(4,[]),(0,verdicts(list(reversed(groups[2:]))))]})
    output=private_output('out.json')
    assert cli.run_classifier(json.dumps({'groups':groups,'evidence':{}}),str(output))==0
    assert calls==[('g1','g2'),('g3','g4'),('g3','g4')]
    assert [r['group_id'] for r in json.loads(output.read_text())]==['g1','g2','g3','g4']
    assert output.stat().st_mode & 0o777 == 0o600


def test_cancellation_is_not_retried_or_published(tmp_path,monkeypatch):
    groups=[group(n) for n in range(1,5)]
    calls=scripted_dispatch(monkeypatch,{('g1','g2'):[(7,[])]})
    output=private_output('out.json');output.write_text('previous')
    assert cli.run_classifier(json.dumps({'groups':groups,'evidence':{}}),str(output))==7
    assert calls==[('g1','g2')]
    assert output.read_text()=='previous'
