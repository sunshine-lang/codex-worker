import asyncio
import hashlib
import json

import httpx

from codex_remote_worker.app import create_app
from codex_remote_worker.common import canonical,digest
from test_translation_tasks import JsonRunner, headers, row, uploads, wait_for_task


def test_out_of_order_batch_is_available_before_whole_task_and_survives_missed_polls(settings):
    async def exercise():
        cfg = settings.model_copy(update={'max_concurrent_jobs': 4, 'task_batch_concurrency': 4})
        release = asyncio.Event()

        class Gated(JsonRunner):
            async def run(self, job, root, touch, stopped, redactor):
                packet = json.loads((root/'work'/job['inputs']['source_file']['filename']).read_text())
                if packet['rows'][0]['row_id'] == '1':
                    await release.wait()
                return await super().run(job, root, touch, stopped, redactor)

        runner = Gated()
        app = create_app(cfg, runner=runner)
        data = {'project_name': 'Streaming', 'segments': [row(0, True), *[row(i) for i in range(1, 66)]]}
        files = uploads(data)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(cfg)) as client:
                try:
                    cap = (await client.get('/v1/tasks/capabilities')).json()
                    assert cap['batch_results'] and cap['batch_result_contract'] == 'translation-task-batches/1'
                    accepted = (await client.post('/v1/tasks', files=files, data={'options': '{}'})).json()
                    task_id = accepted['task_id']
                    for _ in range(500):
                        manifest = (await client.get(f'/v1/tasks/{task_id}/batches')).json()
                        if manifest['batches'][1]['available']:
                            break
                        await asyncio.sleep(.02)
                    else:
                        raise AssertionError('Second batch never became available')
                    assert not manifest['batches'][0]['available']
                    assert manifest['tm_reused_row_ids'] == ['0']
                    assert manifest['source_sha256'] == hashlib.sha256(files['source_file'][1]).hexdigest()
                    assert (await client.get(f'/v1/tasks/{task_id}/result')).status_code == 409
                    assert (await client.get(f'/v1/tasks/{task_id}/batches/0')).status_code == 409
                    batch = manifest['batches'][1]
                    response = await client.get(batch['result_path'])
                    assert hashlib.sha256(response.content).hexdigest() == batch['sha256']
                    assert [r['row_id'] for r in response.json()['rows']] == batch['row_ids']
                    release.set()
                    await wait_for_task(client, task_id)
                    later = (await client.get(f'/v1/tasks/{task_id}/batches')).json()
                    assert all(b['available'] for b in later['batches'])
                    full=(await client.get(f'/v1/tasks/{task_id}/result')).json()
                    assert later['result_content_sha256']==digest(canonical(full))
                    assert later['batches'][1] == batch
                    assert (await client.get(batch['result_path'])).content == response.content
                    assert (await client.get(f'/v1/tasks/{task_id}/batches/-1')).status_code == 404
                    assert (await client.get(f'/v1/tasks/{task_id}/batches/99')).status_code == 404
                    assert (await client.get(f'/v1/tasks/{task_id}/batches', headers={'Authorization': 'Bearer wrong'})).status_code == 401
                    path = app.state.translation_tasks.root/task_id/'results/batch_00001.json'
                    path.write_text('{"rows":[]}')
                    assert (await client.get(batch['result_path'])).status_code == 409
                    path.unlink()
                    assert (await client.get(batch['result_path'])).status_code == 409
                finally:
                    release.set()
    asyncio.run(exercise())


def test_all_tm_manifest_has_no_model_batches(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                accepted = (await client.post('/v1/tasks', files=uploads([row(i, True) for i in range(3)]), data={'options': '{}'})).json()
                await wait_for_task(client, accepted['task_id'])
                manifest = (await client.get(f'/v1/tasks/{accepted["task_id"]}/batches')).json()
                assert manifest['batches'] == [] and manifest['tm_reused_row_ids'] == ['0', '1', '2']
                assert runner.calls == []
    asyncio.run(exercise())
