import asyncio
import copy
import json

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner, text_thread_parameters
from codex_remote_worker.common import canonical, digest
from codex_remote_worker.contextual_tm import CONTEXTUAL_TM, contextual_candidates, resolve_tm_choice, verify_tm_report
from codex_remote_worker.project_session import ProjectSession
from codex_remote_worker.translation_tasks import BatchPolicy, plan_request
from test_project_session import ProjectRpc, read_args
from test_translation_tasks import JsonRunner, headers, uploads, wait_for_task


def ref(key, source, target, context=None):
    return {'id': key, 'source_text': source, 'target_text': target, 'match_rate': 100, 'context': context}


def segment(key, source, context, refs):
    return {'segment_id': key, 'source_language': 'en', 'target_language': 'zh', 'source_text': source,
            'target_text': '', 'context': context, 'tb_list': [], 'tm_list': refs}


def order():
    button = 'File menu / open button'
    return {'project_name': 'tm-fixture', 'segments': [
        segment('button', 'Open', button, [ref('old-open', 'Open', '打开', button)]),
        segment('state', 'Open', 'Door / state value', [ref('old-open', 'Open', '打开', button)]),
        segment('field', 'DD_Revision', None, [ref('code', 'DD_REVISION', 'DD_REVISION'), ref('label', 'DD_Revision', 'DD 修订版')]),
        segment('device', 'Reset the CPU.', None, [ref('old-plc', 'Reset the PLC.', '复位 PLC。')]),
        segment('new', 'Close the door.', None, []),
    ]}


def test_direct_route_requires_evidence_and_order_is_not_a_tiebreaker():
    original = order()
    plan = plan_request(original, BatchPolicy(), tm_policy=CONTEXTUAL_TM)
    assert set(plan['locked']) == {'0'} and plan['pending'] == [1, 2, 3, 4]
    assert len(plan_request(original, BatchPolicy())['locked']) == 4
    reversed_order = copy.deepcopy(original)
    reversed_order['segments'][2]['tm_list'].reverse()
    assert plan_request(reversed_order, BatchPolicy(), tm_policy=CONTEXTUAL_TM)['records'] == plan['records']
    assert 'candidate_id' not in original['segments'][0]['tm_list'][0]
    for source, target in [('Set {0} to 24 V.', '设为 12 V。'), ('Set 24 V.', '设为 24 A。'),
                           ('Set {}.', '设置。'), ('CPU', 'PLC'), ('Code S7-400', '型号 S7-300')]:
        r = segment('x', source, 'panel / identifier', [ref('x', source, target, 'panel / identifier')])
        assert not plan_request(r, BatchPolicy(), tm_policy=CONTEXTUAL_TM)['locked']
    for field, value in [('context', None), ('context', 'UI'), ('source_text', 'OPEN')]:
        r = copy.deepcopy(original['segments'][0])
        r[field] = value
        assert not plan_request(r, BatchPolicy(), tm_policy=CONTEXTUAL_TM)['locked']
    r = copy.deepcopy(original['segments'][0])
    r['tb_list'] = [{'source_text': 'Open', 'target_text': '开启'}]
    assert not plan_request(r, BatchPolicy(), tm_policy=CONTEXTUAL_TM)['locked']
    r['tb_list'] = []
    r['tm_list'][0]['target_language'] = 'de'
    assert not plan_request(r, BatchPolicy(), tm_policy=CONTEXTUAL_TM)['locked']


def test_selection_rejects_disabled_wrong_or_unbound_candidates():
    row = order()['segments'][3]
    r = contextual_candidates(row)[0]
    choice = {'action': 'reuse', 'candidate_id': r['candidate_id']}
    with pytest.raises(ValueError, match='PROTECTED_CONTENT'):
        resolve_tm_choice(row, '', choice, fill_reuse=True)
    with pytest.raises(ValueError, match='TM_REUSE_CHANGED'):
        resolve_tm_choice(row, '其他', choice)
    row['tm_list'][0]['enabled'] = False
    with pytest.raises(ValueError, match='TM_DECISION_INVALID'):
        resolve_tm_choice(row, '', choice, fill_reuse=True)
    with pytest.raises(ValueError, match='TM_DECISION_INVALID'):
        resolve_tm_choice(row, '新译文', {'action': 'retranslate', 'candidate_id': 'invented'})


def test_all_direct_contextual_task_uses_no_model(settings):
    settings = settings.model_copy(update={'project_context_enabled': True, 'continuous_project_session': True})
    runner = JsonRunner()
    data = order()
    data['segments'] = data['segments'][:1]
    async def exercise():
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            res = await client.post('/v1/tasks', files=uploads(data), data={'options': json.dumps({'tm_policy': CONTEXTUAL_TM})})
            final = await wait_for_task(client, res.json()['task_id'])
            assert final['status'] == 'completed' and final['tm_summary']['direct_reused'] == 1
            assert final['progress']['model_required_segments'] == 0 and runner.calls == []
            assert final['self_review_status'] == 'not_required_all_tm'
    asyncio.run(exercise())


class ChoiceRpc(ProjectRpc):
    async def request(self, method, params, **kwargs):
        if method != 'turn/start':
            return await super().request(method, params, **kwargs)
        self.calls.append((method, params))
        self.turns += 1
        self.turn_id = 'turn-' + str(self.turns)
        session = self.runner.project_session
        output = []
        for key in session.pending:
            row = session.records[int(key)]
            action, candidate_id = 'retranslate', ''
            if row['segment_id'] == 'state':
                action, candidate_id, target = 'adapt', row['tm_list'][0]['candidate_id'], '已打开'
            elif row['segment_id'] == 'field':
                chosen = next(r for r in row['tm_list'] if r['id'] == 'label')
                action, candidate_id, target = 'reuse', chosen['candidate_id'], ''
            else:
                target = '复位 CPU。' if row['segment_id'] == 'device' else '关闭门。'
            output.append({'row_id': key, 'target_text': target, 'term_decisions': [],
                           'tm_decision': {'action': action, 'candidate_id': candidate_id}})
        self.actions = [('project_read', read_args(count=0, char_limit=0)),
                        ('project_write', {'rows': output, 'notes': ''}),
                        ('project_read', read_args(count=0, char_limit=0)),
                        ('project_read', read_args('checks')),
                        ('project_review', {'row_ids': [str(i) for i in range(len(session.records))], 'note': ''})]
        self._emit()
        return {'turn': {'id': self.turn_id}}


@pytest.mark.parametrize('reverse', [False, True])
def test_contextual_order_publishes_bound_decisions(settings, monkeypatch, project_markdown_runtime, reverse):
    settings = settings.model_copy(update={'project_context_enabled': True, 'continuous_project_session': True})
    settings.doc_converter.write_bytes(b'Markdown fixture')
    monkeypatch.setattr(TextAppServerRunner, 'baseline_ready', lambda _: {
        'ready': True, 'service_version': 'fixture', 'codex_version': 'fixture', 'tools': {}})
    runner = TextAppServerRunner(settings)
    runner.rpc, runner.started, runner.generation = ChoiceRpc(runner), True, 1
    data = order()
    if reverse:
        data['segments'][2]['tm_list'].reverse()

    async def exercise():
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            assert CONTEXTUAL_TM in (await client.get('/v1/tasks/capabilities')).json()['tm_policies']
            path = '/v1/tasks'
            options = {'options': json.dumps({'tm_policy': CONTEXTUAL_TM})}
            response = await client.post(path, files=uploads(data), data=options)
            assert response.status_code == 202, response.text
            assert response.json()['tm_policy'] == CONTEXTUAL_TM
            task_id = response.json()['task_id']
            assert (await client.post(path, files=uploads(data), data=options)).json()['task_id'] == task_id
            assert (await client.post(path, files=uploads(data), data={'options': '{}'})).status_code == 409
            final = await wait_for_task(client, task_id)
            assert final['status'] == 'completed', final
            manifest = (await client.get(f'{path}/{task_id}/batches')).json()
            assert manifest['tm_reused_row_ids'] == ['0']
            result = (await client.get(final['result_path'])).json()
            targets = [r['target_text'] for r in result['segments']]
            assert targets == ['打开', '已打开', 'DD 修订版', '复位 CPU。', '关闭门。']
            decisions = manifest['tm_decisions']
            assert decisions['2']['upstream_id'] == 'label'
            assert decisions['2']['enabled_tm_index'] == (0 if reverse else 1)
            assert final['tm_decisions_sha256'] == digest(canonical(decisions))
            assert verify_tm_report(data['segments'], targets, decisions, {'0'}) == final['tm_summary'] == {
                'direct_reused': 1, 'agent_reused': 1, 'agent_adapted': 1, 'agent_retranslated': 1}
            assert final['progress']['model_required_segments'] == 4 and final['progress']['finished_segments'] == 5
            assert runner.rpc.threads == 1 and runner.rpc.turns == 1
            session = runner.project_session
            assert ProjectSession(settings, session.root).values == session.values
            schema = text_thread_parameters(settings, session.root, project_session=True, contextual_tm=True)
            write = next(t for t in schema['dynamicTools'] if t['name'] == 'project_write')
            assert 'tm_decision' in write['inputSchema']['properties']['rows']['items']['required']
            assert 'Only rows marked locked_tm=true' in schema['developerInstructions']
            tampered = copy.deepcopy(decisions)
            tampered['2']['candidate_id'] = decisions['0']['candidate_id']
            with pytest.raises(ValueError, match='TM_DECISION_INVALID'):
                verify_tm_report(data['segments'], targets, tampered, {'0'})
    asyncio.run(exercise())
