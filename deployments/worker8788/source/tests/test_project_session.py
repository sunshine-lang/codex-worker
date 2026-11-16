import asyncio
import json
import sqlite3

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner, text_thread_parameters
from codex_remote_worker.common import JobError, file_digest, write_json
from codex_remote_worker.project_session import ProjectSession, TOOL_NAMES
from codex_remote_worker.translation_tasks import plan_request, BatchPolicy
from test_app_server_adapter import Rpc
from test_translation_tasks import row, uploads, headers, wait_for_task


def read_args(operation='source', **kw):
    return {'operation':operation,'start':0,'count':32,'query':'','char_offset':0,**kw}


def translated(key, text='保持柜门打开。'):
    return {'row_id':str(key),'target_text':text,'term_decisions':[]}


def fixture_session(settings, count=3):
    task_id, job_id = 'task_'+'a'*32, 'job_'+'b'*32
    directory = settings.data_root/'tasks'/task_id
    root = settings.data_root/'jobs'/job_id
    (root/'control').mkdir(parents=True)
    data = {'project_name':'session-test','segments':[row(i,i==1) for i in range(count)]}
    write_json(directory/'plan.json',plan_request(data,BatchPolicy()))
    task = {'task_id':task_id,'session_job_id':job_id,'continuous_project_session':True,
        'plan_sha256':file_digest(directory/'plan.json')}
    with sqlite3.connect(settings.data_root/'tasks.sqlite3') as db:
        db.execute('create table tasks(task_id text,record text)')
        db.execute('insert into tasks values(?,?)',(task_id,json.dumps(task)))
    write_json(root/'control/project-session.json',{'task_id':task_id,'job_id':job_id})
    return ProjectSession(settings,root)


def read_all(session):
    for start in range(0,len(session.records),32):
        response=session.call('project_read',read_args(start=start))
        while response['has_more']:
            response=session.call('project_read',read_args(start=start,char_offset=response['next_char_offset']))


def check_all(session):
    start=0
    while True:
        response=session.call('project_read',read_args('checks',start=start))
        parts=[response['content']]
        while response['has_more']:
            response=session.call('project_read',read_args('checks',start=start,char_offset=response['next_char_offset']))
            parts.append(response['content'])
        value=json.loads(''.join(parts))
        if value['next_start'] is None:
            return
        start=value['next_start']


def test_full_project_read_past_translation_revisions_and_review_versions(settings):
    session=fixture_session(settings)
    assert session.progress()['reviewed_segments']==0
    with pytest.raises(ValueError,match='Read'):
        session.call('project_write',{'rows':[translated(0)],'notes':''})
    read_all(session)
    with pytest.raises(ValueError,match='order'):
        session.call('project_write',{'rows':[translated(2)],'notes':''})
    session.call('project_write',{'rows':[translated(0),translated(2)],'notes':'Earlier context: cabinet door.'})
    value=json.loads(session.call('project_read',read_args('search',query='保持柜门打开'))['content'])
    assert len(value['rows'])==2
    session.begin_review()
    with pytest.raises(ValueError,match='CURRENT'):
        session.call('project_review',{'row_ids':['0'],'note':''})
    read_all(session)
    session.call('project_review',{'row_ids':['0','1','2'],'note':'same Codex review'})
    session.call('project_write',{'rows':[translated(0,'请保持柜门打开。')],'notes':'Revised after reading later context.'})
    with pytest.raises(JobError,match='检查'):
        session.finish()
    with pytest.raises(ValueError,match='CURRENT'):
        session.call('project_review',{'row_ids':['0'],'note':''})
    read_all(session)
    session.call('project_review',{'row_ids':['0'],'note':'All current versions checked.'})
    check_all(session)
    result=session.finish()
    assert result['rows']==[translated(0,'请保持柜门打开。'),translated(2)]
    assert session.progress()['reviewed_segments']==3
    assert session.revisions==1


def test_tool_bounds_locking_and_project_binding(settings):
    session=fixture_session(settings)
    read_all(session)
    for rows in ([translated(1)],[translated(999)],[translated(0),translated(0)]):
        with pytest.raises(ValueError):session.call('project_write',{'rows':rows,'notes':''})
    with pytest.raises(ValueError):session.call('project_read',{**read_args(),'task_id':'another'})
    with pytest.raises(ValueError):session.call('project_read',read_args(start=-1))
    with pytest.raises(JobError):session.call('shell',{'cmd':'anything'})
    write_json(session.root/'control/project-session.json',{'task_id':'../../another','job_id':session.root.name})
    with pytest.raises(JobError):ProjectSession(settings,session.root)


def test_partial_source_page_cannot_be_treated_as_read(settings):
    session=fixture_session(settings)
    session.records[0]['context']='very long context '*4000
    value=session.call('project_read',read_args(count=1))
    assert value['has_more']
    with pytest.raises(ValueError,match='complete'):
        session.call('project_write',{'rows':[translated(0)],'notes':''})
    offset=value['next_char_offset']
    while value['has_more']:
        value=session.call('project_read',read_args(count=1,char_offset=offset))
        offset=value['next_char_offset']
    session.call('project_write',{'rows':[translated(0)],'notes':''})


class ProjectRpc(Rpc):
    def __init__(self,runner,mode='success'):
        super().__init__(mode)
        self.runner=runner
        self.tool_threads=set()
        self.messages=[]
    async def request(self,method,params,**kw):
        if method!='turn/start':
            return await super().request(method,params,**kw)
        self.calls.append((method,params))
        self.turns+=1;self.turn_id='turn-'+str(self.turns)
        session=self.runner.project_session
        session.page_chars=100000  # Pagination itself is covered by the long-source test.
        # Simulate model tool requests through the real runner transport dispatch.
        self.actions=[]
        for query in ('prompt','project_style','general_style'):
            self.actions.append(('project_read',read_args('guides',query=query)))
        for start in range(0,len(session.records),32):
            self.actions.append(('project_read',read_args(start=start)))
        if session.phase=='translating':
            self.actions.append(('project_write',{'rows':[translated(k,'模型译文 '+k) for k in session.pending],'notes':'One project.'}))
        elif self.mode=='bad_review':
            self.actions=[]
        else:
            self.actions.append(('project_write',{'rows':[translated(session.pending[0],'回改后的译文')],'notes':'Corrected earlier text.'}))
            self.actions.append(('project_read',read_args()))
            for start in range(0,len(session.records),128):
                self.actions.append(('project_review',{'row_ids':[str(i) for i in range(start,min(start+128,len(session.records)))],'note':'reviewed'}))
            self.actions.append(('project_read',read_args('checks')))
        self._emit()
        return {'turn':{'id':self.turn_id}}
    def _emit(self):
        queue=self.queues[self.thread_id]
        base={'threadId':self.thread_id,'turnId':self.turn_id}
        if self.actions:
            tool,args=self.actions.pop(0)
            self.current=(tool,args)
            queue.put_nowait({'id':str(len(self.actions)), 'method':'item/tool/call','params':{**base,'tool':tool,'arguments':args}})
        else:
            status='draft_complete' if self.runner.project_session.phase=='translating' else 'review_complete'
            if status=='draft_complete':
                queue.put_nowait({'method':'item/completed','params':{**base,'item':{'id':'compact','type':'contextCompaction'}}})
            queue.put_nowait({'method':'item/completed','params':{**base,'item':{'id':self.turn_id,'type':'agentMessage',
                'phase':'final_answer','text':json.dumps({'status':status,'summary':'done'})}}})
            queue.put_nowait({'method':'turn/completed','params':{**base,'turn':{'id':self.turn_id,'status':'completed','items':[]}}})
    async def _write(self,value):
        assert value['result']['success'],value
        self.messages.append(value)
        if self.current[0]=='project_read' and self.current[1]['operation']=='checks':
            outer=json.loads(value['result']['contentItems'][0]['text'])
            inner=json.loads(outer['content'])
            if inner.get('next_start') is not None:
                self.actions.insert(0,('project_read',read_args('checks',start=inner['next_start'])))
        self._emit()


@pytest.mark.parametrize('mode',['success','bad_review'])
def test_one_job_one_thread_two_turns_and_no_unreviewed_result(settings,monkeypatch,mode):
    settings=settings.model_copy(update={'project_context_enabled':True,'continuous_project_session':True})
    settings.doc_converter.write_bytes(b'fixture converter (not called for Markdown)')
    monkeypatch.setattr(TextAppServerRunner,'baseline_ready',lambda _: {'ready':True,'service_version':'fixture','codex_version':'fixture','tools':{}})
    runner=TextAppServerRunner(settings)
    runner.rpc=ProjectRpc(runner,mode);runner.started=True;runner.generation=1
    async def exercise():
        app=create_app(settings,runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(settings)) as c:
                data={'project_name':'one-session','segments':[row(i,i==1) for i in range(65)]}
                response=await c.post('/v1/tasks',files=uploads(data),data={'options':'{}'})
                assert response.status_code==202,response.text
                task_id=response.json()['task_id']
                final=await wait_for_task(c,task_id)
                manifest=(await c.get('/v1/tasks/'+task_id+'/batches')).json()
                assert len(manifest['batches'])==2
                assert len({b['job_id'] for b in manifest['batches']})==1
                assert runner.rpc.threads==1 and runner.rpc.turns==2 and runner.rpc.unloaded==1
                starts=[p for m,p in runner.rpc.calls if m=='turn/start']
                assert starts[0]['threadId']==starts[1]['threadId']
                assert 'uploaded_prompt' in starts[0]['input'][0]['text']
                assert 'uploaded_prompt' not in starts[1]['input'][0]['text']
                if mode=='success':
                    assert final['status']=='completed',final
                    assert final['self_review_status']=='completed'
                    job=app.state.store.get(manifest['batches'][0]['job_id'])
                    assert job['execution']['context_compactions']==1
                    guide_reads=[x for x in runner.project_session.calls if x.get('operation')=='guides']
                    assert len(guide_reads)==6 and guide_reads[-1]['phase']=='reviewing'
                    result=(await c.get(final['result_path'])).json()
                    assert result['segments'][0]['target_text']=='回改后的译文'
                    assert result['segments'][1]['target_text']=='TM 原样译文'
                    for b in manifest['batches']:
                        assert b['available']
                        assert (await c.get(b['result_path'])).status_code==200
                    assert result['segments'][-1]['segment_id']=='64'
                else:
                    assert final['status']=='failed',final
                    assert not any(b['available'] for b in manifest['batches'])
                    assert (await c.get('/v1/tasks/'+task_id+'/result')).status_code==409
    asyncio.run(exercise())


def test_session_tool_policy_has_no_filesystem_or_external_app_access(settings,tmp_path):
    (tmp_path/'work').mkdir()
    params=text_thread_parameters(settings,tmp_path,project_session=True)
    assert {t['name'] for t in params['dynamicTools']}==TOOL_NAMES
    assert params['environments']==[] and params['runtimeWorkspaceRoots']==[]
    assert params['config']['features.shell_tool'] is False
    assert params['config']['features.multi_agent'] is False
    assert params['config']['web_search']=='live'
