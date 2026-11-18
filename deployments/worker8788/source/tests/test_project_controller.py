import asyncio
import json

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.app_server_rpc import AppServerRpcError
from codex_remote_worker.common import JobError, write_json
from codex_remote_worker.project_controller import ProjectController
from codex_remote_worker.project_session import ProjectSession
from codex_remote_worker.project_recovery import queue_resume
from test_project_session import fixture_session, read_all, translated, check_all, ProjectRpc, read_args
from test_translation_tasks import row, uploads, headers, wait_for_task


def test_checkpoint_reloads_rows_and_versions_without_rewriting_the_file(settings):
    session = fixture_session(settings, 8)
    read_all(session)
    session.call('project_write', {'rows':[translated(i) for i in (0,2,3)], 'notes':'durable decision'})
    path = session.root/'control/project-session-state.json'
    before = path.read_bytes()
    restored = ProjectSession(settings, session.root)
    assert path.read_bytes() == before
    assert restored.values == session.values and restored.versions == session.versions
    assert restored.notes == 'durable decision' and restored.restored
    assert restored.overview()['first_untranslated_row_id'] == '4'
    assert not restored.source_seen
    with pytest.raises(ValueError, match='complete'):
        restored.call('project_write', {'rows':[translated(0,'改译')], 'notes':''})


@pytest.mark.parametrize('corruption', ['binding', 'plan', 'row', 'version', 'approval'])
def test_invalid_checkpoint_is_never_cleared(settings, corruption):
    session = fixture_session(settings)
    read_all(session)
    session.call('project_write', {'rows':[translated(0)], 'notes':''})
    path = session.root/'control/project-session-state.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if corruption=='binding': data['job_id']='another'
    if corruption=='plan': data['plan_sha256']='another'
    if corruption=='row': data['translations']['0']['row_id']='9'
    if corruption=='version': data['versions']['0']=0
    if corruption=='approval': data['reviewed_versions']['0']=99
    write_json(path,data)
    before=path.read_bytes()
    with pytest.raises(JobError): ProjectSession(settings,session.root)
    assert path.read_bytes()==before


def test_whole_phase_scope_exceeds_32_and_false_completion_keeps_progress(settings):
    s=fixture_session(settings,70)
    controller=ProjectController(s)
    first=json.loads(controller.request())
    assert first['work_scope']=='whole_remaining_project'
    assert first['remaining_rows']==69
    assert not s.source_seen  # No host-selected material window; model chooses the read scope.
    read_all(s)
    assert 'assigned_row_ids' not in first
    subset=s.pending[:5]
    s.call('project_write',{'rows':[translated(k) for k in subset],'notes':'partial'})
    assert controller.completed(b'{"status":"draft_complete","summary":"context limit"}')
    second=json.loads(controller.request())
    assert not set(subset)&set(controller.remaining())
    assert second['remaining_rows']==64
    assert s.phase=='translating' and len(s.values)==5
    assert controller.stalled==0


def test_review_recovery_and_completion_require_current_versions_and_checks(settings):
    s=fixture_session(settings)
    read_all(s)
    s.call('project_write',{'rows':[translated(k) for k in s.pending],'notes':''})
    s.begin_review();read_all(s)
    s.call('project_review',{'row_ids':['0','1'],'note':'part reviewed'})
    restored=ProjectSession(settings,s.root)
    controller=ProjectController(restored)
    request=json.loads(controller.request())
    assert request['remaining_rows']==1
    read_all(restored)
    restored.call('project_review',{'row_ids':['2'],'note':'rest checked'})
    assert controller.completed(b'not a status JSON')  # Still needs rule checks.
    controller.request();check_all(restored)
    assert not controller.completed(b'')
    result=restored.finish()
    assert len(result['rows'])==2 and restored.progress()['reviewed_segments']==3


def test_oversized_row_keeps_lossless_pagination_with_exact_next_read(settings):
    s=fixture_session(settings)
    s.records[0]['context']='large source '*10000
    controller=ProjectController(s)
    request=json.loads(controller.request())
    assert request['remaining_rows']==2 and 'complete_source_entries' not in request
    assert not s.source_seen
    args=read_args(count=1)
    first=s.call('project_read',args)
    assert first['next_read']['char_offset']==first['next_char_offset']
    response=first
    while response['has_more']:
        response=s.call('project_read',response['next_read'])
    assert '0' in s.source_seen


class RangeRpc(ProjectRpc):
    async def request(self, method, params, **kw):
        if method!='turn/start':
            return await super().request(method,params,**kw)
        self.calls.append((method,params));self.turns+=1;self.turn_id='turn-'+str(self.turns)
        s=self.runner.project_session
        self.actions=[]
        assigned=self.runner.project_controller.remaining()
        self.actions=[('project_read',read_args(count=0,char_limit=0))]
        if s.phase=='translating':
            # Simulate a model stopping early in every turn, despite declaring completion.
            self.actions.append(('project_write',{'rows':[translated(k,'模型译文 '+k) for k in assigned[:11]],'notes':'range'}))
        elif assigned:
            self.actions.append(('project_review',{'row_ids':assigned,'note':'reviewed current entries'}))
        else:
            self.actions=[('project_read',read_args('checks'))]
        self._emit()
        return {'turn':{'id':self.turn_id}}


def test_early_model_endings_finish_one_project_in_many_turns(settings,monkeypatch,project_markdown_runtime):
    settings=settings.model_copy(update={'project_context_enabled':True,'continuous_project_session':True})
    settings.doc_converter.write_bytes(b'fixture converter')
    monkeypatch.setattr(TextAppServerRunner,'baseline_ready',lambda _: {'ready':True,'service_version':'fixture','codex_version':'fixture','tools':{}})
    runner=TextAppServerRunner(settings);runner.rpc=RangeRpc(runner);runner.started=True;runner.generation=1
    async def exercise():
        app=create_app(settings,runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(settings)) as c:
                data={'project_name':'bounded','segments':[row(i,i==1) for i in range(65)]}
                response=await c.post('/v1/tasks',files=uploads(data),data={'options':'{}'})
                task=await wait_for_task(c,response.json()['task_id'])
                assert task['status']=='completed',task
                assert runner.rpc.threads==1 and runner.rpc.turns>2
                assert runner.project_session.progress()['reviewed_segments']==65
                result=(await c.get(task['result_path'])).json()
                assert len(result['segments'])==65 and result['segments'][1]['target_text']=='TM 原样译文'
                assert len({p['threadId'] for m,p in runner.rpc.calls if m=='turn/start'})==1
    asyncio.run(exercise())


class ResumeRpc(ProjectRpc):
    async def request(self,method,params,**kw):
        if method=='thread/resume':
            self.calls.append((method,params));self.thread_id=params['threadId']
            self.cwd=self.runner.project_session.root/'work'
            assert 'dynamicTools' not in params and 'ephemeral' not in params
            assert params['environments']==[] and params['runtimeWorkspaceRoots']==[]
            if self.mode=='resume_failed':
                raise JobError('APP_SERVER_RPC_ERROR','fixture resume refused before model startup')
            if self.mode.startswith('resume_environment_unavailable') or self.mode=='resume_other_error':
                detail = ('error resuming thread: failed to load AGENTS.md instructions for environment `local`: '
                    'failed to prepare fs sandbox: failed to prepare Windows sandbox: '
                    'app runtime provisioning service is unavailable; refusing helper fallback')
                raise AppServerRpcError(-32603,detail if self.mode.startswith('resume_environment_unavailable') else 'connection reset')
            return {'thread':{'id':self.thread_id},'activePermissionProfile':{'id':'translation'},
                    'approvalPolicy':'never','model':'server/default-model','modelProvider':'fixture',
                    'reasoningEffort':'high','cwd':params['cwd']}
        result=await super().request(method,params,**kw)
        if method=='thread/start':
            self.thread_id='recovered-'+self.thread_id
            result['thread']['id']=self.thread_id
            if self.mode=='resume_environment_unavailable_then_stall':
                self.mode='bad_review'
        return result


@pytest.mark.parametrize('recovery_mode',['same_thread','resume_environment_unavailable',
    'resume_environment_unavailable_then_stall','resume_other_error'])
def test_failed_project_resumes_same_thread_and_retains_saved_draft(settings,monkeypatch,recovery_mode,project_markdown_runtime):
    settings=settings.model_copy(update={'project_context_enabled':True,'continuous_project_session':True})
    settings.doc_converter.write_bytes(b'fixture converter')
    monkeypatch.setattr(TextAppServerRunner,'baseline_ready',lambda _: {'ready':True,'service_version':'fixture','codex_version':'fixture','tools':{}})
    runner=TextAppServerRunner(settings);runner.rpc=ProjectRpc(runner,'bad_review');runner.started=True;runner.generation=1
    async def fail_then_resume():
        app=create_app(settings,runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(settings)) as c:
                response=await c.post('/v1/tasks',files=uploads({'project_name':'resume','segments':[row(i,i==1) for i in range(4)]}),data={'options':'{}'})
                task_id=response.json()['task_id'];failed=await wait_for_task(c,task_id)
                assert failed['status']=='failed' and failed['error']['code']=='PROJECT_NO_PROGRESS'
        state=(runner.project_session.root/'control/project-session-state.json').read_bytes()
        recovered=queue_resume(settings,task_id)
        assert recovered['restored_rows']==3
        assert (runner.project_session.root/'control/project-session-state.json').read_bytes()==state
        assert not (settings.data_root/'.maintenance.json').exists()
        with pytest.raises(JobError,match='未结束'):
            queue_resume(settings,task_id)
        refused=TextAppServerRunner(settings);refused_rpc=ResumeRpc(refused,'resume_failed');refused.rpc=refused_rpc;refused.started=True;refused.generation=1
        refused_app=create_app(settings,runner=refused)
        async with refused_app.router.lifespan_context(refused_app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(refused_app),base_url='http://worker',headers=headers(settings)) as c:
                refusal=await wait_for_task(c,task_id)
                assert refusal['status']=='failed' and refusal['error']['code']=='APP_SERVER_RPC_ERROR'
                assert refusal['codex_thread_id']==failed['codex_thread_id']
                assert refused_rpc.turns==0
        assert queue_resume(settings,task_id)['thread_id']==failed['codex_thread_id']
        restarted=TextAppServerRunner(settings);restarted_rpc=ResumeRpc(restarted,recovery_mode);restarted.rpc=restarted_rpc;restarted.started=True;restarted.generation=1
        app2=create_app(settings,runner=restarted)
        async with app2.router.lifespan_context(app2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app2),base_url='http://worker',headers=headers(settings)) as c:
                final=await wait_for_task(c,task_id)
                if recovery_mode=='resume_other_error':
                    assert final['status']=='failed' and final['error']['code']=='APP_SERVER_RPC_ERROR'
                    assert restarted_rpc.turns==0 and restarted_rpc.threads==0
                    return
                if recovery_mode=='resume_environment_unavailable_then_stall':
                    assert final['status']=='failed' and final['error']['code']=='PROJECT_NO_PROGRESS'
                    assert final['codex_thread_id']!=failed['codex_thread_id']
                    recovered_again=queue_resume(settings,task_id)
                    assert recovered_again['thread_id']==final['codex_thread_id']
                    assert recovered_again['restored_rows']==3
                    return
                assert final['status']=='completed',final
                assert restarted_rpc.turns==1
                assert [p for m,p in restarted_rpc.calls if m=='thread/resume']
                if recovery_mode=='same_thread':
                    assert final['codex_thread_id']==failed['codex_thread_id']
                    assert restarted_rpc.threads==0
                else:
                    assert final['codex_thread_id']!=failed['codex_thread_id']
                    start=[p for m,p in restarted_rpc.calls if m=='thread/start'][0]
                    assert start['environments']==[] and start['runtimeWorkspaceRoots']==[]
                    assert start['permissions']=='translation' and start['approvalPolicy']=='never'
                    audit=json.loads((runner.project_session.root/'control/project-thread-recovery.json').read_text())
                    assert audit['source_thread_id']==failed['codex_thread_id']
                    assert audit['restored_project_session']['draft_segments']==3
                    assert not audit['history_restored']
                result=(await c.get(final['result_path'])).json()
                assert len(result['segments'])==4
    asyncio.run(fail_then_resume())
