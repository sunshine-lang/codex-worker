import asyncio
import copy
import json
import uuid

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError
from codex_remote_worker.translation_tasks import BatchPolicy, TaskStore, plan_request


def test_project_order_chain_survives_retry_cancel_and_reopen(tmp_path):
    db = tmp_path/'tasks.sqlite3'
    store = TaskStore(db)

    def accept(task_id, key, project, replaces=None):
        stage = tmp_path/('stage-'+task_id)
        stage.mkdir()
        record = dict(task_id=task_id, request_key=key, project_id=project, project_name='same display name',
                      status='queued', created_at='2026-01-01', fingerprint=key)
        if replaces:
            record['replaces_task_id'] = replaces
        return store.accept(record,stage,tmp_path/task_id,10)

    try:
        first, _ = accept('first','A','project-one')
        other, _ = accept('other','B','project-two')
        assert 'previous_project_task_id' not in first and 'previous_project_task_id' not in other
        duplicate, reused = accept('duplicate','A','project-one')
        assert reused and duplicate['task_id'] == 'first'
        second, _ = accept('second','C','project-one')
        assert store.project_predecessor(second)['task_id'] == 'first'
        second['status'] = 'stopped'
        store.save(second)
    finally:
        store.close()
    store = TaskStore(db)
    try:
        third, _ = accept('third','D','project-one')
        assert third['previous_project_task_id'] == 'second'
        assert store.project_predecessor(third)['task_id'] == 'second'
        revised, _ = accept('revised','E','project-one','second')
        assert store.project_predecessor(revised)['task_id'] == 'first'
        assert store.project_predecessor(third)['task_id'] == 'revised'
        retry, reused = accept('retry', 'E', 'project-one', 'second')
        assert reused and retry['task_id'] == 'revised'
        revised['status'] = 'failed'
        store.save(revised)
        last, _ = accept('final', 'F', 'project-one', 'revised')
        assert last['order_position_id'] == 'second'
        assert store.project_predecessor(third)['task_id'] == 'final'
        fourth, _ = accept('fourth','G','project-one')
        assert store.project_predecessor(fourth)['task_id'] == 'third'
        last['status'] = 'failed'
        store.save(last)
        third['session_job_id'] = 'historical-job'
        store.save(third)
        with pytest.raises(JobError, match='已有后单'):
            accept('too-late','H','project-one','final')
    finally:
        store.close()


def row(index=0, tm=False):
    return {'segment_id': str(index), 'source_language': 'en', 'target_language': 'zh',
            'source_text': 'Open the server settings.', 'target_text': 'old candidate', 'context': 'UI',
            'tb_list': [{'source_text': 'server', 'target_text': '服务器'}],
            'tm_list': [{'source_text': 'Open the server settings.', 'target_text': 'TM 原样译文', 'match_rate': 100}] if tm else []}


class JsonRunner:
    def __init__(self, fail=False, incomplete=False):
        self.calls = []
        self.fail, self.incomplete = fail, incomplete
    def ready(self):
        return {'ready': True}
    def recover(self, job):
        return True
    async def run(self, job, root, touch, stopped, redactor):
        packet = json.loads((root/'work'/job['inputs']['source_file']['filename']).read_text())
        self.calls.append(packet)
        if self.fail:
            raise JobError('FIXTURE_FAILURE', 'fixture failed')
        assert packet['phase'] == 'translate' and packet['tm_reuse'] == []
        assert all(not any(t.get('match_rate') == 100 for t in r['record']['tm_list']) for r in packet['rows'])
        value = {'rows': [{'row_id': r['row_id'], 'target_text': '模型译文 '+r['row_id'], 'term_decisions': []} for r in packet['rows']]}
        (root/'control/final_response.raw.md').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return {'exit_code': 0, 'turn_completed': not self.incomplete, 'turn_failed': False,
                'trace_complete': True, 'thread_id': job['job_id']}


def uploads(data, prompt='Translate using the supplied guides.'):
    return {'source_file': ('request.json', json.dumps(data, ensure_ascii=False).encode(), 'application/json'),
            'prompt_file': ('prompt.md', prompt.encode(), 'text/plain'),
            'project_style': ('style.md', b'Project style.', 'text/plain'),
            'general_style': ('general.md', b'General style.', 'text/plain')}


def headers(settings, key=None):
    return {'Authorization': 'Bearer '+settings.service_token.get_secret_value(), 'Idempotency-Key': key or str(uuid.uuid4())}


async def wait_for_task(client, task_id):
    for _ in range(500):
        response = await client.get('/v1/tasks/'+task_id)
        response.raise_for_status()
        value = response.json()
        if value['status'] in ('completed', 'failed'):
            return value
        await asyncio.sleep(.02)
    raise AssertionError('Task did not finish')


def test_8788_project_names_bind_once_and_reuse_legacy_workspaces(tmp_path):
    store = TaskStore(tmp_path/'tasks.sqlite3')

    def submit(name, project_id=None, legacy=False, key=None, policy=None):
        task = uuid.uuid4().hex
        stage = tmp_path/('stage-'+task)
        stage.mkdir()
        record = dict(task_id=task, request_key=key or task, project_name=name, project_id=project_id,
                      status='queued', created_at='2026-01-01', tm_policy=policy, fingerprint=key or task)
        return store.accept(record, stage, tmp_path/task, 100, project_name_key=None if legacy else name.strip())

    try:
        first, _ = submit('旧项目', 'old-id', legacy=True)
        submit('重名', 'old-1', legacy=True)
        submit('重名', 'old-2', legacy=True)
        resumed, _ = submit(' 旧项目 ')
        assert resumed['project_id'] == 'old-id' and resumed['previous_project_task_id'] == first['task_id']
        for name, explicit, error in [('重名', None, 'PROJECT_NAME_AMBIGUOUS'), ('旧项目','different','PROJECT_NAME_CONFLICT')]:
            with pytest.raises(JobError) as exc:
                submit(name, explicit)
            assert exc.value.code == error
        a, _ = submit('Name', key='repeat')
        duplicate, reused = submit('Name', key='repeat')
        assert reused and duplicate['task_id'] == a['task_id']
        with pytest.raises(JobError) as exc:
            submit('Name', policy='contextual-v2')
        assert exc.value.code == 'PROJECT_TOOLSET_CHANGED'
        other, _ = submit('name')
        assert other['project_id'] != a['project_id'] and 'previous_project_task_id' not in other
        store.close()
        store = TaskStore(tmp_path/'tasks.sqlite3')
        after, _ = submit(' Name ')
        assert after['project_id'] == a['project_id'] and after['previous_project_task_id'] == a['task_id']
    finally:
        store.close()


def test_resume_replays_its_receipt_and_protects_live_child(settings):
    from unittest.mock import Mock
    from codex_remote_worker.translation_tasks import TaskManager

    async def exercise():
        cfg = settings.model_copy(update={'port':8788, 'continuous_project_session':True})
        jobs, worker = Mock(), Mock()
        jobs.by_key.return_value = None
        manager = TaskManager(cfg, jobs, worker)
        def add(key):
            tid, stage = manager.new_stage()
            task = dict(task_id=tid, project_name='P', request_key=key, fingerprint=key,
                        created_at='2026-01-01', status='queued', continuous_project_session=True, batches=[{}])
            return manager.store.accept(task, stage, manager.root/tid, 10, project_name_key='P')[0]
        try:
            a, b, c = [add(key) for key in ('A','B','C')]
            stopped = await manager.request_stop(b['task_id'])
            assert stopped['status']=='stopped' and manager.store.project_predecessor(c)['task_id']==b['task_id']
            jobs.by_key.return_value = {'job_id':'hidden', 'status':'running'}
            with pytest.raises(JobError) as exc:
                await manager.request_resume(b['task_id'], 'key')
            assert exc.value.code == 'ORDER_STILL_RUNNING'
            jobs.by_key.return_value = None
            reply = await manager.request_resume(b['task_id'], 'key')
            assert reply['previous_project_task_id']==a['task_id'] and reply['status']=='queued'
            assert (await manager.request_stop(b['task_id']))['status']=='stopped'
            assert (await manager.request_resume(b['task_id'], 'key'))['status']=='stopped'
            assert (await manager.request_resume(b['task_id'], 'another-key'))['status']=='queued'
            assert manager.store.project_predecessor(c)['task_id']==b['task_id']
        finally:
            manager.store.close()
    asyncio.run(exercise())


def test_name_only_packets_share_a_project_and_bare_rows_stay_independent(settings):
    async def exercise():
        cfg = settings.model_copy(update={'port':8788, 'continuous_project_session':True, 'project_context_enabled':True})
        runner = JsonRunner()
        app = create_app(cfg, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(cfg)) as client:
                async def submit(source):
                    response = await client.post('/v1/tasks', files=uploads(source), data={'options':'{}'}, headers=headers(cfg))
                    assert response.status_code == 202, response.text
                    return response.json()

                rows = [row(0, tm=True)]
                first = await submit({'project_name':'translation-mvp', 'segments':rows})
                bare = await submit(rows)
                after = await submit({'project_name':' translation-mvp ', 'segments':rows})
                assert 'project_id' not in bare and 'previous_project_task_id' not in bare
                assert after['project_id'] == first['project_id']
                assert after['previous_project_task_id'] == first['task_id']
                for order in (first, bare, after):
                    assert (await wait_for_task(client, order['task_id']))['status'] == 'completed'
                result = (await client.get(after['status_path']+'/result')).json()
                assert result['project_name'] == ' translation-mvp '
                assert runner.calls == []
    asyncio.run(exercise())


def test_mixed_request_only_two_rows_reach_model(settings):
    async def exercise():
        data = {'project_name': 'MVP', 'segments': [row(0, True), row(1), row(2, True), row(3), row(4, True)]}
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                cap = await client.get('/v1/tasks/capabilities')
                assert cap.json()['contract'] == 'translation-task/1'
                submitted = await client.post('/v1/tasks', files=uploads(data), data={'options': '{}'})
                assert submitted.status_code == 202, submitted.text
                accepted = submitted.json()
                assert accepted['progress']['tm_reused_segments'] == 3
                final = await wait_for_task(client, accepted['task_id'])
                assert final['status'] == 'completed', final
                result = (await client.get(final['result_path'])).json()
                assert len(runner.calls) == 1
                assert [r['row_id'] for r in runner.calls[0]['rows']] == ['1', '3']
                assert final['progress']['finished_segments'] == 5
                assert final['review_status'] == 'not_started'
                for before, after in zip(data['segments'], result['segments']):
                    assert {k:v for k,v in before.items() if k!='target_text'} == {k:v for k,v in after.items() if k!='target_text'}
                    assert after['target_text'] != 'old candidate'
                assert result['segments'][0]['target_text'] == 'TM 原样译文'
    asyncio.run(exercise())


def test_all_tm_zero_model_and_idempotent_retry(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        data = {'project_name': 'All TM', 'segments': [row(i, True) for i in range(25)]}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                first, retry = await asyncio.gather(*(client.post('/v1/tasks', files=uploads(data), data={'options':'{}'}) for _ in range(2)))
                assert {first.status_code, retry.status_code} == {200, 202}
                task_id = first.json()['task_id']
                assert retry.json()['task_id'] == task_id
                final = await wait_for_task(client, task_id)
                assert final['progress']['total_batches'] == 0 and final['progress']['percent'] == 100
                assert runner.calls == []
                conflict = await client.post('/v1/tasks', files=uploads(data, prompt='Different'), data={'options':'{}'})
                assert conflict.status_code == 409
                fresh = await client.post('/v1/tasks', files=uploads(data), data={'options':'{}'}, headers=headers(settings))
                assert fresh.status_code == 202 and fresh.json()['task_id'] != task_id
    asyncio.run(exercise())


def test_multibatch_merge_preserves_order(settings):
    async def exercise():
        data = {'project_name': 'Many', 'segments': [row(i, i % 11 == 0) for i in range(70)]}
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads(data), data={'options':'{}'})
                assert response.status_code == 202, response.text
                final = await wait_for_task(client, response.json()['task_id'])
                assert final['status'] == 'completed', final
                result = (await client.get(final['result_path'])).json()
                expected = [str(i) for i in range(70) if i % 11]
                assert [r['row_id'] for packet in runner.calls for r in packet['rows']] == expected
                assert [r['segment_id'] for r in result['segments']] == [str(i) for i in range(70)]
                assert final['progress']['model_translated_segments'] == len(expected)
    asyncio.run(exercise())


def test_5000_rows_batch_policy_and_input_unchanged():
    data = {'project_name':'Large', 'segments':[row(i, i % 5 == 0) for i in range(5000)]}
    before = copy.deepcopy(data)
    plan = plan_request(data, BatchPolicy())
    assert len(plan['locked']) == 1000
    assert [i for batch in plan['batches'] for i in batch] == [i for i in range(5000) if i % 5]
    assert all(len(batch) <= 32 for batch in plan['batches'])
    assert data == before
    long = {'project_name':'Long', 'segments':[row(0), {**row(1), 'source_text':'x'*40000}, row(2)]}
    assert plan_request(long, BatchPolicy())['batches'] == [[0], [1], [2]]


@pytest.mark.parametrize('rate', [True, '100', -1, 101])
def test_invalid_tm_rejected_without_model(settings, rate):
    async def exercise():
        data = row(tm=True)
        data['tm_list'][0]['match_rate'] = rate
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads(data), data={'options':'{}'})
                assert response.status_code == 422, response.text
                assert runner.calls == []
    asyncio.run(exercise())


@pytest.mark.parametrize('incomplete', [False, True])
def test_failed_or_unproven_execution_never_returns_complete_result(settings, incomplete):
    async def exercise():
        runner = JsonRunner(fail=not incomplete, incomplete=incomplete)
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                submitted = await client.post('/v1/tasks', files=uploads([row(0, True), row(1)]), data={'options':'{}'})
                task_id = submitted.json()['task_id']
                final = await wait_for_task(client, task_id)
                assert final['status'] == 'failed'
                assert final['progress']['finished_segments'] == 1
                assert (await client.get('/v1/tasks/'+task_id+'/result')).status_code == 409
                assert len(runner.calls) == 1
    asyncio.run(exercise())


def test_restart_recovers_child_accepted_before_parent_saved_job_id(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            manager = app.state.translation_tasks
            manager.stopping = True
            manager.wakeup.set()
            await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads([row(0)]), data={'options':'{}'})
                task_id = response.json()['task_id']
                task = manager.store.get(task_id)
                child = await asyncio.to_thread(manager._enqueue, task, task['batches'][0])
                duplicate = await asyncio.to_thread(manager._enqueue, task, task['batches'][0])
                assert duplicate['job_id'] == child['job_id']
                app.state.worker.wakeup.set()
                for _ in range(100):
                    if app.state.store.get(child['job_id'])['status'] == 'completed':
                        break
                    await asyncio.sleep(.02)
                assert app.state.store.get(child['job_id'])['status'] == 'completed'
        app2 = create_app(settings, runner=runner)
        async with app2.router.lifespan_context(app2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app2), base_url='http://worker', headers=headers(settings)) as client:
                final = await wait_for_task(client, task_id)
                assert final['status'] == 'completed', final
                assert len(runner.calls) == 1
    asyncio.run(exercise())


def test_task_auth_and_server_owned_batch_policy(settings):
    async def exercise():
        app = create_app(settings, runner=JsonRunner())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker') as client:
                assert (await client.get('/v1/tasks/capabilities')).status_code == 401
                response = await client.post('/v1/tasks', files=uploads([row()]), data={'options':'{"max_rows":5000}'}, headers=headers(settings))
                assert response.status_code == 422
    asyncio.run(exercise())
