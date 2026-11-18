import asyncio
import json
import sqlite3

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner, text_thread_parameters
from codex_remote_worker.common import JobError, digest, file_digest, write_json
from codex_remote_worker.project_session import ProjectSession, TOOL_NAMES
from codex_remote_worker.translation_tasks import plan_request, BatchPolicy
from test_app_server_adapter import Rpc
from test_translation_tasks import row, uploads, headers, wait_for_task


def read_args(operation='source', **kw):
    return {'operation':operation,'start':0,'count':32,'query':'','char_offset':0,'char_limit':16000,**kw}


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


def test_model_selects_full_material_and_unbounded_save_review_groups(settings):
    session=fixture_session(settings,131)
    session.records[0]['context']='long source context '*5000
    read=session.call('project_read',read_args(count=0,char_limit=0))
    assert not read['has_more'] and read['next_read'] is None
    assert len(read['content'])>65536 and len(json.loads(read['content'])['rows'])==131
    # The model can choose any order; an incomplete checkpoint restores without filling gaps.
    session.call('project_write',{'rows':[translated(130)],'notes':'last row first'})
    restored=ProjectSession(settings,session.root)
    assert set(restored.values)=={'130'}
    assert restored.overview()['first_untranslated_row_id']=='0'
    session.call('project_write',{'rows':[translated(k) for k in reversed(session.pending)],'notes':'all rows'})
    assert session.phase=='reviewing'
    with pytest.raises(ValueError,match='CURRENT'):
        session.call('project_review',{'row_ids':[str(i) for i in range(131)],'note':''})
    session.call('project_read',read_args(count=0,char_limit=0))
    session.call('project_review',{'row_ids':[str(i) for i in range(131)],'note':'current versions'})
    session.call('project_read',read_args('checks',count=0,char_limit=0))
    result=session.finish()
    assert [r['row_id'] for r in result['rows']]==session.pending


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
        # Simulate model tool requests through the real runner transport dispatch.
        self.actions=[]
        for query in ('prompt','project_style','general_style'):
            self.actions.append(('project_read',read_args('guides',query=query)))
        for start in range(0,len(session.records),32):
            self.actions.append(('project_read',read_args(start=start,char_limit=0)))
        if session.phase=='translating':
            self.actions.append(('project_write',{'rows':[translated(k,'模型译文 '+k) for k in session.pending],'notes':'One project.'}))
        if self.mode=='same_turn':
            self.actions.append(('project_read',read_args(count=0,char_limit=0)))
        if self.mode=='bad_review' and session.phase=='reviewing':
            self.actions=[]
        elif session.phase=='reviewing' or self.mode=='same_turn':
            self.actions.append(('project_write',{'rows':[translated(session.pending[0],'回改后的译文')],'notes':'Corrected earlier text.'}))
            self.actions.append(('project_read',read_args(char_limit=0)))
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
            if self.turns==1:
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


@pytest.mark.parametrize('mode',['success','bad_review','same_turn'])
def test_one_job_one_thread_model_selects_turns_and_no_unreviewed_result(settings,monkeypatch,mode,project_markdown_runtime):
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
                expected_turns=4 if mode=='bad_review' else 1 if mode=='same_turn' else 2
                assert runner.rpc.threads==1 and runner.rpc.turns==expected_turns and runner.rpc.unloaded==1
                starts=[p for m,p in runner.rpc.calls if m=='turn/start']
                assert len({p['threadId'] for p in starts})==1
                assert all('outputSchema' not in p for p in starts)
                assert '"status": "phase_complete"' in starts[0]['input'][0]['text']
                assert 'uploaded_prompt' in starts[0]['input'][0]['text']
                assert all('uploaded_prompt' not in p['input'][0]['text'] for p in starts[1:])
                if mode!='bad_review':
                    assert final['status']=='completed',final
                    assert final['self_review_status']=='completed'
                    job=app.state.store.get(manifest['batches'][0]['job_id'])
                    delivery=json.loads((runner.project_session.root/'control/context-delivery.json').read_text(encoding='utf-8'))
                    submitted=starts[0]['input'][0]['text'].encode('utf-8')
                    assert delivery['bytes']==len(submitted)
                    assert delivery['sha256']==digest(submitted)
                    assert job['execution']['context_compactions']==1
                    assert job['execution']['project_controller_version']=='ledger/2'
                    assert all(t['work_scope']=='whole_remaining_project' for t in job['execution']['project_turns'])
                    # The host accepts a complete draft AND self-review in a single model turn.
                    assert job['execution']['project_turns'][0]['after']['draft']==64
                    assert job['execution']['project_turns'][-1]['after']['reviewed']==65
                    guide_reads=[x for x in runner.project_session.calls if x.get('operation')=='guides']
                    assert len(guide_reads)==3*expected_turns
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


def test_status_json_without_tool_writes_cannot_complete_draft(settings):
    runner = TextAppServerRunner(settings)
    runner.project_session = fixture_session(settings)
    for _ in range(2):
        assert runner.next_turn('thread', runner.project_session.root,
            b'{"status":"draft_complete","summary":"done"}') is not None
    with pytest.raises(JobError) as error:
        runner.next_turn('thread', runner.project_session.root,
            b'{"status":"draft_complete","summary":"done"}')
    assert error.value.code == 'PROJECT_NO_PROGRESS'
    assert runner.project_session.phase == 'translating'
    assert not runner.project_session.values


@pytest.mark.parametrize('payload', [b'not JSON', b'{"status":"draft_complete"}',
    b'{"status":"review_complete","summary":"done"}',
    b'{"status":"draft_complete","summary":1}'])
def test_invalid_status_is_only_a_hint_and_cannot_publish_an_incomplete_ledger(settings, payload):
    runner = TextAppServerRunner(settings)
    runner.project_session = fixture_session(settings)
    assert runner.next_turn('thread', runner.project_session.root, payload) is not None
    assert runner.project_session.phase == 'translating'
    assert not runner.project_session.values


def test_session_tool_policy_has_no_filesystem_or_external_app_access(settings,tmp_path):
    (tmp_path/'work').mkdir()
    params=text_thread_parameters(settings,tmp_path,project_session=True)
    assert {t['name'] for t in params['dynamicTools']}==TOOL_NAMES
    assert params['environments']==[] and params['runtimeWorkspaceRoots']==[]
    assert params['config']['features.shell_tool'] is False
    assert params['config']['features.multi_agent'] is False
    assert params['config']['web_search']=='live'
