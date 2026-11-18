import asyncio
import json

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.project_activity import activity_progress
from codex_remote_worker.project_session import ProjectSession
from test_project_session import ProjectRpc, fixture_session, read_args, translated
from test_translation_tasks import JsonRunner, headers, row, uploads, wait_for_task


def test_journal_uses_actual_writes_and_survives_resume(settings):
    session = fixture_session(settings)
    with pytest.raises(ValueError):
        session.call('project_write', {'rows': [translated(0)], 'notes': 'failed'})
    assert session.recent_actions() == []
    session.call('project_read', read_args(char_limit=1))
    assert session.recent_actions()[-1]['paged'] is True
    session.call('project_read', read_args(char_limit=0))
    session.call('project_write', {'rows': [translated(0)], 'notes': 'private-notes'})
    assert session.recent_actions()[-1]['action'] == 'save_translation'
    session.call('project_write', {'rows': [translated(0, '已修订')], 'notes': 'private-notes'})
    revision = session.recent_actions()[-1]
    assert revision['action'] == 'revise_translation' and revision['seq'] == 4
    assert revision['segment_ids'] == [session.records[0]['segment_id']]
    assert revision['checkpoint_file'] == 'project-session-state.json'
    session.call('project_read', read_args('search', query='private-search'))
    before = session.recent_actions()
    assert [e['seq'] for e in before] == [2, 3, 4, 5]
    assert not any(s in json.dumps(before, ensure_ascii=False) for s in ('private-notes', 'private-search'))
    resumed = ProjectSession(settings, session.root)
    assert resumed.recent_actions() == before
    resumed.call('project_write', {'rows': [], 'notes': 'notes only'})
    assert resumed.recent_actions()[-1]['seq'] == 6
    assert resumed.recent_actions()[-1]['action'] == 'save_notes'
    assert 'recent_actions' not in resumed.overview()  # Model inputs keep their existing shape.


def test_empty_read_and_older_checkpoint_do_not_invent_actions(settings):
    session = fixture_session(settings)
    session.call('project_read', read_args(start=100))
    assert '为空' in session.recent_actions()[-1]['message']
    path = session.root / 'control/project-session-tools.json'
    old = json.loads(path.read_text(encoding='utf-8'))
    del old['calls'][0]['activity']
    path.write_text(json.dumps(old), encoding='utf-8')
    restored = ProjectSession(settings, session.root)
    assert restored.recent_actions() == []
    restored.call('project_read', read_args('overview'))
    assert restored.recent_actions()[-1]['seq'] == 2
    assert activity_progress({'status': 'failed', 'session_progress': {'tool_calls': 1}})['recent_actions'] == []


def test_status_http_serves_the_running_then_published_actions(settings, monkeypatch, project_markdown_runtime):
    settings = settings.model_copy(update={'project_context_enabled': True, 'continuous_project_session': True})
    settings.doc_converter.write_bytes(b'fixture for markdown guides')
    monkeypatch.setattr(TextAppServerRunner, 'baseline_ready', lambda _: {
        'ready': True, 'service_version': 'fixture', 'codex_version': 'fixture', 'tools': {}})

    runner = TextAppServerRunner(settings)
    rpc = ProjectRpc(runner, 'same_turn')
    runner.rpc, runner.started, runner.generation = rpc, True, 1

    async def exercise():
        waiting, release = asyncio.Event(), asyncio.Event()
        write = rpc._write

        async def pause_second_tool(reply):
            if rpc.current[1].get('query') == 'project_style':
                waiting.set()
                await release.wait()
            return await write(reply)

        rpc._write = pause_second_tool
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            assert (await client.get('/v1/tasks/capabilities')).json()['task_recent_actions'] is True
            body = {'project_name': 'activity fixture', 'segments': [row(i, i == 1) for i in range(3)]}
            response = await client.post('/v1/tasks', files=uploads(body), data={'options': '{}'})
            assert response.status_code == 202, response.text
            task_id = response.json()['task_id']
            path = '/v1/tasks/' + task_id
            params = {'include_actions': 'true'}
            try:
                async with asyncio.timeout(10):
                    await waiting.wait()
                    while True:
                        current = (await client.get(path, params=params)).json()
                        actions = (current.get('session_progress') or {}).get('recent_actions')
                        if actions:
                            break
                        await asyncio.sleep(.05)
                assert current['status'] == 'running'
                assert [(a['seq'], a['actor'], a['action'], a['file_name']) for a in actions] == [
                    (1, 'agent', 'read_document', 'prompt_file.md')]
                assert actions[0]['document_role'] == 'prompt_file'
                assert (await client.get(path, params=params)).json()['session_progress']['recent_actions'] == actions
                assert 'recent_actions' not in (await client.get(path)).json()['session_progress']
            finally:
                release.set()
            assert (await wait_for_task(client, task_id))['status'] == 'completed'
            final = (await client.get(path, params=params)).json()['session_progress']
            actions = final['recent_actions']
            assert len(actions) == 4 and actions[-1]['seq'] == final['tool_calls'] + 1
            assert actions[-1]['actor'] == 'worker' and actions[-1]['file_name'] == 'result.json'
            assert actions[-1]['action'] == 'create_result'
            assert any(a['action'] == 'self_check' for a in actions)
            assert (await client.get(path + '/result')).status_code == 200
            assert (await client.get(path, params=params)).json()['session_progress']['recent_actions'] == actions

    asyncio.run(exercise())


def test_pure_tm_status_only_records_the_real_result_file(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
            response = await client.post('/v1/tasks', files=uploads({'project_name': 'tm', 'segments': [row(0, True)]}),
                                         data={'options': '{}'})
            task_id = response.json()['task_id']
            assert (await wait_for_task(client, task_id))['session_progress'] is None
            final = (await client.get('/v1/tasks/' + task_id, params={'include_actions': True})).json()
            assert runner.calls == []
            action, = final['session_progress']['recent_actions']
            assert (action['seq'], action['actor'], action['action']) == (1, 'worker', 'create_result')

    asyncio.run(exercise())
