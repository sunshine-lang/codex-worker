import asyncio
import copy
import json

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.common import JobError, write_json
from codex_remote_worker.contextual_tm import CONTEXTUAL_TM
from codex_remote_worker.json_delivery import read_json_source
from codex_remote_worker.qa import check_row
from codex_remote_worker.segment_languages import LANGUAGE_INSTRUCTIONS, PER_SEGMENT
from codex_remote_worker.translation_tasks import BatchPolicy, batch_packet, batch_prompt, plan_request
from test_project_session import ProjectRpc, fixture_session, read_args, translated
from test_translation_tasks import JsonRunner, headers, row, uploads, wait_for_task


def mixed_order():
    return {'project_name': 'mixed-interface', 'segments': [
        {'segment_id': 'JP-17', 'source_language': 'ja', 'target_language': 'en',
         'source_text': '設定を保存してください。', 'target_text': '', 'context': 'UI', 'tb_list': [], 'tm_list': []},
        {'segment_id': 'TW-42', 'source_language': 'en', 'target_language': 'zh-TW',
         'source_text': 'Open the file and save the settings.', 'target_text': '', 'context': 'UI', 'tb_list': [], 'tm_list': []}]}


def test_plan_and_internal_packet_preserve_language_values(tmp_path):
    order = mixed_order()
    for source, target in [('en', 'zh'), ('zh-CN', 'zh-TW'), ('fr-CA', 'de-CH'), ('Japanese', 'English')]:
        order['segments'].append({**order['segments'][0], 'segment_id': str(len(order['segments'])),
                                  'source_language': source, 'target_language': target})
    plan = plan_request(order, BatchPolicy(), language_mode=PER_SEGMENT)
    packet = batch_packet(plan, plan['pending'], 'mixed')
    path = tmp_path / 'request.json'
    write_json(path, packet)
    checked = read_json_source(path)
    assert checked['language_mode'] == PER_SEGMENT
    assert [(r['record']['source_language'], r['record']['target_language']) for r in checked['rows']] == [
        (r['source_language'], r['target_language']) for r in order['segments']]
    assert LANGUAGE_INSTRUCTIONS in batch_prompt('a guide', language_mode=PER_SEGMENT)
    assert LANGUAGE_INSTRUCTIONS not in batch_prompt('a guide')
    with pytest.raises(JobError, match='en'):
        plan_request(order, BatchPolicy())


@pytest.mark.parametrize('key,value', [('source_language', None), ('target_language', '  '), ('source_language', 7)])
def test_missing_language_identifies_segment_at_each_boundary(tmp_path, key, value):
    order = mixed_order()
    plan = plan_request(order, BatchPolicy(), language_mode=PER_SEGMENT)
    packet = batch_packet(plan, plan['pending'], 'mixed')
    for row in (order['segments'][1], packet['rows'][1]['record']):
        if value is None:
            del row[key]
        else:
            row[key] = value
    with pytest.raises(JobError, match='TW-42.*' + key):
        plan_request(order, BatchPolicy(), language_mode=PER_SEGMENT)
    path = tmp_path / 'request.json'
    write_json(path, packet)
    with pytest.raises(JobError, match='TW-42.*' + key):
        read_json_source(path)


def test_language_scoped_repetition_and_style_checks(settings):
    session = fixture_session(settings)
    for i, target in ((0, 'zh-CN'), (2, 'zh-TW')):
        session.records[i].update(source_text='Save', target_language=target)
    session.call('project_read', read_args(count=0, char_limit=0))
    session.call('project_write', {'rows': [translated('0', '保存'), translated('2', '儲存')], 'notes': ''})
    def repeats():
        result = session.call('project_read', read_args('checks', count=0, char_limit=0))
        return [c for c in json.loads(result['content'])['checks'] if c['rule'] == 'repeated_source_variation']
    assert not repeats()
    session.records[2]['target_language'] = 'zh-CN'
    assert repeats()
    record = {'id':'0', 'excel_row':1, 'source':'Select 保存 {0}.', 'terms':[]}
    english = check_row(record, 'Click "保存", then continue.', target_language='en')
    chinese = check_row(record, '按 "保存", 然后继续。', target_language='zh-TW')
    assert 'placeholder' in {v['rule'] for v in english}
    assert 'punctuation' not in {v['rule'] for v in english}
    assert 'punctuation' in {v['rule'] for v in chinese}


def test_same_submission_cannot_switch_language_mode(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        original = {'project_name':'mode-retry', 'segments':[row(0, True)]}
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            legacy = await client.post('/v1/tasks', files=uploads(original), data={'options':'{}'})
            assert legacy.status_code == 202 and 'language_mode' not in legacy.json()
            switched = await client.post('/v1/tasks', files=uploads(original),
                                         data={'options':json.dumps({'language_mode':PER_SEGMENT})})
            assert switched.status_code == 409
            assert switched.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
            final = await wait_for_task(client, legacy.json()['task_id'])
            assert final['status'] == 'completed' and not runner.calls
    asyncio.run(exercise())


async def fixture_translations(records):
    choices = {('ja', 'en'): 'Please save the settings.', ('en', 'zh-TW'): '開啟檔案並儲存設定。'}
    return [choices[r['source_language'], r['target_language']] for r in records]


class MixedRpc(ProjectRpc):
    """Drive project tools with translations of the records actually returned by project_read."""
    def __init__(self, runner, translator=fixture_translations):
        super().__init__(runner, 'same_turn')
        self.translator, self.read_records = translator, []

    async def request(self, method, params, **kwargs):
        if method != 'turn/start':
            return await super().request(method, params, **kwargs)
        self.calls.append((method, params))
        self.turns += 1
        self.turn_id = 'turn-' + str(self.turns)
        self.actions = [('project_read', read_args(count=0, char_limit=0))]
        self._emit()
        return {'turn': {'id': self.turn_id}}

    async def _write(self, value):
        if not self.read_records and self.current[0] == 'project_read':
            assert value['result']['success']
            result = json.loads(value['result']['contentItems'][0]['text'])
            entries = json.loads(result['content'])['rows']
            self.read_records = [e['record'] for e in entries]
            output = await self.translator(self.read_records)
            rows = [translated(entry['row_id'], target) for entry, target in zip(entries, output, strict=True)]
            if self.runner.project_session.contextual_tm:
                for r in rows:
                    r['tm_decision'] = {'action':'retranslate', 'candidate_id':''}
            self.actions += [('project_write', {'rows':rows, 'notes':''}),
                             ('project_read', read_args(count=0, char_limit=0)),
                             ('project_read', read_args('checks')),
                             ('project_review', {'row_ids':[r['row_id'] for r in rows], 'note':''})]
        return await super()._write(value)


@pytest.mark.parametrize('tm_policy', [None, CONTEXTUAL_TM])
def test_worker_http_mixed_order_reaches_agent_and_round_trips(settings, monkeypatch, project_markdown_runtime, tm_policy):
    settings = settings.model_copy(update={'project_context_enabled': True, 'continuous_project_session': True})
    settings.doc_converter.write_bytes(b'Markdown fixture')
    monkeypatch.setattr(TextAppServerRunner, 'baseline_ready', lambda _: {
        'ready': True, 'service_version': 'fixture', 'codex_version': 'fixture', 'tools': {}})
    runner = TextAppServerRunner(settings)
    runner.rpc, runner.started, runner.generation = MixedRpc(runner), True, 1

    async def exercise():
        original = mixed_order()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            assert PER_SEGMENT in (await client.get('/v1/tasks/capabilities')).json()['language_modes']
            opts = {'language_mode': PER_SEGMENT}
            if tm_policy:
                opts['tm_policy'] = tm_policy
            body = {'options':json.dumps(opts)}
            reply = await client.post('/v1/tasks', files=uploads(original), data=body)
            assert reply.status_code == 202, reply.text
            task = reply.json()
            assert task['language_mode'] == PER_SEGMENT and task.get('tm_policy') == tm_policy
            assert (await client.post('/v1/tasks', files=uploads(original), data=body)).json()['task_id'] == task['task_id']
            done = await wait_for_task(client, task['task_id'])
            assert done['status'] == 'completed', done
            assert done['progress']['model_required_segments'] == 2
            result = (await client.get(done['result_path'])).json()
            expected = copy.deepcopy(original)
            for r, target in zip(expected['segments'], await fixture_translations(original['segments'])):
                r['target_text'] = target
            assert result == expected
            assert [(r['segment_id'], r['source_language'], r['target_language']) for r in runner.rpc.read_records] == [
                ('JP-17', 'ja', 'en'), ('TW-42', 'en', 'zh-TW')]
            start = next(params for method, params in runner.rpc.calls if method == 'thread/start')
            assert LANGUAGE_INSTRUCTIONS in start['developerInstructions']
            manifest = (await client.get('/v1/tasks/' + task['task_id'] + '/batches')).json()
            assert manifest['language_mode'] == manifest['task']['language_mode'] == PER_SEGMENT
            assert runner.rpc.threads == runner.rpc.turns == 1
    asyncio.run(exercise())
