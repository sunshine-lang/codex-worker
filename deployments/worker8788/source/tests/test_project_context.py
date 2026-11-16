import asyncio
import json
import sqlite3

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_rpc import AppServerRpc
from codex_remote_worker.common import JobError, Redactor, file_digest, write_json
from codex_remote_worker.project_context import ProjectContext
from test_translation_tasks import JsonRunner, row, uploads, headers, wait_for_task


def args(operation='overview', **kw):
    return dict(operation=operation,query='',start=0,count=10,scope='all',char_offset=0,**kw) if not kw else {**args(operation),**kw}


def fixture_project(settings, *, unfinished=False):
    task_id='task_'+'a'*32
    directory=settings.data_root/'tasks'/task_id
    directory.mkdir(parents=True)
    job=settings.data_root/'jobs'/('job_'+'b'*32)
    (job/'control').mkdir(parents=True)
    records=[row(i) for i in range(5000)]
    records[4]['source_text']='Leave it open.'
    records[4999]['source_text']='Leave it open.'
    write_json(directory/'plan.json',{'records':records,'locked':{},'project_name':'one project'})
    write_json(directory/'results/batch_00000.json',{'rows':[{'row_id':'4','target_text':'让柜门保持打开。','term_decisions':[]}]})
    record={'task_id':task_id,'plan_sha256':file_digest(directory/'plan.json'),
        'batches':[{'indices':[4],'status':'running' if unfinished else 'completed',
            'result_sha256':file_digest(directory/'results/batch_00000.json')},
            {'indices':[4999],'job_id':job.name}]}
    with sqlite3.connect(settings.data_root/'tasks.sqlite3') as db:
        db.execute('create table tasks(task_id text,record text)')
        db.execute('insert into tasks values(?,?)',(task_id,json.dumps(record)))
    write_json(job/'control/project-context.json',{'task_id':task_id,'batch_index':1,'job_id':job.name})
    return ProjectContext,job,directory


def test_distant_previous_sentence_and_full_project_read(settings):
    _,job,directory=fixture_project(settings)
    context=ProjectContext(settings,job)
    overview=json.loads(context.query(args())['content'])
    example=overview['suggested_previous_examples'][0]
    assert example['entry_number']==5
    assert example['previous_translation']['target_text']=='让柜门保持打开。'
    assert example['previous_translation']['translation_status']=='model_candidate_not_reviewed'
    value=json.loads(context.query(args('read',start=4998,count=2))['content'])
    assert [r['entry_number'] for r in value['rows']]==[4999,5000]
    assert value['rows'][1]['previous_translation'] is None
    hits=json.loads(context.query(args('search',query='Leave it open.',scope='previous'))['content'])
    assert hits['rows'][0]['entry_number']==5
    with pytest.raises(ValueError):context.query({**args(),'task_id':'other'})
    with pytest.raises(ValueError):context.query(args('read',start=-1))
    with pytest.raises(ValueError):context.query(args('search',query=''))


def test_rejects_unfinished_previous_batch_and_changed_results(settings):
    _,job,directory=fixture_project(settings,unfinished=True)
    with pytest.raises(JobError,match='前一批'):ProjectContext(settings,job)
    with sqlite3.connect(settings.data_root/'tasks.sqlite3') as db:
        record=json.loads(db.execute('select record from tasks').fetchone()[0]);record['batches'][0]['status']='completed'
        db.execute('update tasks set record=?',(json.dumps(record),))
    (directory/'results/batch_00000.json').write_text('{}')
    with pytest.raises(JobError,match='历史译文'):ProjectContext(settings,job)


def test_results_are_pageable_without_silent_truncation(settings):
    _,job,directory=fixture_project(settings)
    context=ProjectContext(settings,job)
    context.records[0]['context']='长内容'*10000
    response=context.query(args('read',count=1));parts=[response['content']]
    while response['has_more']:
        response=context.query(args('read',count=1,char_offset=response['next_char_offset']))
        parts.append(response['content'])
    assert json.loads(''.join(parts))['rows'][0]['record']['context']=='长内容'*10000


def test_project_batches_sequential_even_with_eight_global_workers(settings):
    settings=settings.model_copy(update={'project_context_enabled':True,'max_concurrent_jobs':8,'task_batch_concurrency':8})
    class Runner(JsonRunner):
        active=0
        async def run(self,job,root,touch,stopped,redactor):
            self.active+=1
            assert self.active==1
            context=ProjectContext(settings,root)
            assert len(context.previous)==len(self.calls)*32
            assert len(context.records)==65
            await asyncio.sleep(.03)
            result=await super().run(job,root,touch,stopped,redactor)
            self.active-=1
            return result
    async def exercise():
        runner=Runner();app=create_app(settings,runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(settings)) as c:
                response=await c.post('/v1/tasks',files=uploads({'project_name':'sequential','segments':[row(i) for i in range(65)]}),data={'options':'{}'})
                assert response.status_code==202,response.text
                final=await wait_for_task(c,response.json()['task_id'])
                assert final['status']=='completed',final
                assert len(runner.calls)==3
    asyncio.run(exercise())


def test_rpc_only_routes_tools_for_explicitly_bound_threads(settings,tmp_path):
    async def exercise():
        rpc=AppServerRpc(settings,tmp_path,Redactor());sent=[]
        async def write(value):sent.append(value)
        rpc._write=write
        own=rpc.subscribe('own');foreign=rpc.subscribe('foreign');rpc.tool_threads.add('own')
        request={'id':'tool-1','method':'item/tool/call','params':{'threadId':'own','turnId':'turn','tool':'project_context','arguments':args()}}
        await rpc._dispatch(request)
        assert await own.get()==request and not sent
        await rpc._dispatch({**request,'params':{**request['params'],'threadId':'foreign'}})
        assert 'error' in sent[-1]
        assert (await foreign.get())['method']=='worker/interactiveRequestDenied'
        rpc.unsubscribe_local('own');assert 'own' not in rpc.tool_threads
    asyncio.run(exercise())


@pytest.mark.parametrize('fault',[None,'foreign_image','wrong_parent','extra_child','replaced_pid'])
def test_lazy_tool_host_requires_exact_images_and_parentage(settings,tmp_path,fault):
    host=tmp_path/'codex-code-mode-host.exe';host.write_bytes(b'official fixture')
    console=tmp_path/'conhost.exe';console.write_bytes(b'console fixture')
    rpc=AppServerRpc(settings,tmp_path,Redactor())
    rpc.runtime_processes=((10,1.),(11,2.))
    rpc.trusted_tool_images={str(p.resolve()).casefold():file_digest(p) for p in (host,console)}
    class Process:
        metadata={'pid':10}
        def active_processes(self):
            members=((10,1.),(11,2.),(12,3.),(13,4.))
            if fault=='extra_child':members+=((14,5.),)
            if fault=='replaced_pid':members=((10,99.),)+members[1:]
            return members
        def process_details(self,members):
            return {12:{'image':str(host),'parent':999 if fault=='wrong_parent' else 10},
                    13:{'image':str(console),'parent':12}}
    rpc.process=Process()
    if fault=='foreign_image':host.write_bytes(b'changed executable')
    assert rpc.adopt_project_tool_runtime()==(fault is None)
    if fault:assert rpc.runtime_processes==((10,1.),(11,2.))
