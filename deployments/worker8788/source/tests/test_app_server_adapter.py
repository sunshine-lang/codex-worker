import asyncio
import copy
import json
from pathlib import Path

import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_rpc import AppServerRpc, AppServerRpcError
from codex_remote_worker.app_server_runner import AppServerRunner, thread_parameters, verify_thread
from codex_remote_worker.common import JobError, Redactor, digest
from codex_remote_worker.config import read_codex_config
from codex_remote_worker.runner import CodexRunner


class Process:
    metadata = {'pid': 99999999, 'created_at': 0, 'job_object': 'Local\\AppServer-TEST-ONLY'}
    def active_count(self):
        return 1


class Rpc:
    def __init__(self, mode='success'):
        self.mode, self.connected = mode, True
        self.process = Process()
        self.transport_bytes = 0
        self.calls, self.queues = [], {}
        self.threads = self.turns = self.interrupts = self.unloaded = self.closed = 0
    def subscribe(self, thread_id):
        queue = asyncio.Queue()
        self.queues[thread_id] = queue
        return queue
    def unsubscribe_local(self, thread_id):
        self.queues.pop(thread_id, None)
    async def close(self):
        self.closed += 1
        self.connected = False
    def runtime_processes_unchanged(self):
        return self.process.active_count() == 1
    async def request(self, method, params, **kwargs):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/start':
            self.threads += 1
            self.cwd = Path(params['cwd'])
            self.thread_id = 'thread-' + str(self.threads)
            return {'thread': {'id': self.thread_id}, 'activePermissionProfile': {'id':'translation'},
                'approvalPolicy':'never','model':'server/default-model','modelProvider':'fixture',
                'reasoningEffort':'high','cwd':params['cwd']}
        if method == 'turn/start':
            self.turns += 1
            self.turn_id = 'turn-' + str(self.turns)
            queue = self.queues[params['threadId']]
            def event(method, **data):
                queue.put_nowait({'method':method,'params':{'threadId':self.thread_id,'turnId':self.turn_id,**data}})
            if self.mode == 'disconnect':
                self.connected = False
            elif self.mode == 'interactive':
                event('worker/interactiveRequestDenied')
            elif self.mode == 'wrong_turn':
                queue.put_nowait({'method':'turn/completed','params':{'threadId':self.thread_id,'turn':{'id':'foreign-turn','status':'completed'}}})
            elif self.mode != 'blocked':
                final = '{"rows":[{"row_id":"0","target_text":"你好","term_decisions":[]}]}'
                source = self.cwd.parent/'control/source.json'
                if source.exists():
                    packet = json.loads(source.read_text(encoding='utf-8'))
                    final = json.dumps({'rows':[{'row_id':r['row_id'],'target_text':'模型译文 '+r['row_id'],'term_decisions':[]} for r in packet['rows']]},ensure_ascii=False)
                if self.mode == 'bad_json':
                    final = '{"rows":[]}'
                event('item/completed', item={'type':'reasoning','id':'reasoning','summary':['PRIVATE_THOUGHT']})
                event('item/completed', item={'type':'agentMessage','id':'comment','phase':'commentary','text':'not final'})
                if self.mode != 'no_final':
                    event('item/completed', item={'type':'agentMessage','id':'final','phase':'final_answer','text':final})
                event('thread/tokenUsage/updated', tokenUsage={'total':{'inputTokens':50,'cachedInputTokens':10,'outputTokens':20}})
                event('turn/completed', turn={'id':self.turn_id,'status':'failed' if self.mode=='failed' else 'completed','items':[],
                    'error':{'message':'fixture-provider-key'} if self.mode=='failed' else None})
            return {'turn':{'id':self.turn_id,'status':'inProgress','items':[]}}
        if method == 'turn/interrupt':
            self.interrupts += 1
            self.queues[params['threadId']].put_nowait({'method':'turn/completed','params':{'threadId':params['threadId'],
                'turn':{'id':params['turnId'],'status':'interrupted','items':[]}}})
            return {}
        if method == 'thread/unsubscribe':
            self.unloaded += 1
            queue = self.queues.get(params['threadId'])
            if queue is not None:
                queue.put_nowait({'method':'thread/closed','params':{'threadId':params['threadId']}})
            return {'status':'unsubscribed'}
        raise AssertionError(method)


@pytest.fixture
def adapter(settings, monkeypatch):
    from codex_remote_worker import app_server_runner as module
    monkeypatch.setattr(CodexRunner,'ready',lambda self:{'ready':True,'service_version':'0.4.0','codex_version':'fixture','tools':{}})
    async def prepare(*args):
        return {'runtime_version':'fixture','model_called':False}
    monkeypatch.setattr(module,'prepare_task_runtime',prepare)
    runner=AppServerRunner(settings)
    runner.rpc=Rpc()
    runner.started=True
    runner.generation=1
    return runner


def prepared(tmp_path, index=0):
    root=tmp_path/('run-'+str(index))
    for name in ('input','work','control','logs'):
        (root/name).mkdir(parents=True)
    prompt=b'\xef\xbb\xbf Exact prompt\r\n '
    (root/'input/prompt.md').write_bytes(prompt)
    job={'job_id':'job_test_'+str(index),'inputs':{'prompt_file':{'filename':'prompt.md'}},'prompt_sha256':digest(prompt),
         'options':{'timeout_seconds':60}}
    return root,job,prompt


def test_named_permissions_not_legacy_workspace_fallback(settings,tmp_path):
    root,_,_=prepared(tmp_path)
    params=thread_parameters(settings,root)
    assert params['permissions']=='translation' and 'sandbox' not in params
    assert params['approvalPolicy']=='never' and params['allowProviderModelFallback'] is False
    fs=params['config']['permissions.translation.filesystem']
    assert fs[str(settings.data_root)]=='deny'
    assert fs[str(root/'work')]=='write'
    assert fs[str(settings.codex_home)]=='deny'
    assert params['config']['permissions.translation.network.enabled'] is False
    env=params['config']['shell_environment_policy.set']
    assert not any(k in env for k in ('CODEX_HOME','FIXTURE_PROVIDER_KEY','OPENAI_API_KEY'))


@pytest.mark.parametrize('field,value', [('activePermissionProfile',{'id':':workspace'}),('approvalPolicy','on-request'),
    ('model','different'),('modelProvider','different'),('reasoningEffort','low'),('cwd','C:/different')])
def test_thread_refuses_policy_or_model_drift(settings,tmp_path,field,value):
    root,_,_=prepared(tmp_path)
    reply={'thread':{'id':'one'},'activePermissionProfile':{'id':'translation'},'approvalPolicy':'never',
        'cwd':str(root/'work'),'model':'server/default-model','modelProvider':'fixture','reasoningEffort':'high'}
    reply[field]=value
    with pytest.raises(JobError):
        verify_thread(reply,read_codex_config(settings.codex_home),root)


def test_daemon_reused_but_threads_and_results_are_separate(adapter,tmp_path):
    async def run():
        for i in range(2):
            root,job,prompt=prepared(tmp_path,i)
            changes={}
            result=await adapter.run(job,root,lambda **v:changes.update(v),lambda:False,adapter.redactor)
            assert result['turn_completed'] and result['harness']=='codex app-server'
            assert result['daemon_reused'] and result['app_server_generation']==1
            assert json.loads((root/'control/final_response.raw.md').read_text(encoding='utf-8'))['rows'][0]['target_text']=='你好'
            trace=(root/'logs/trace.jsonl').read_text(encoding='utf-8')
            assert 'PRIVATE_THOUGHT' not in trace and 'fixture-provider-key' not in trace
            assert result['usage']['input_tokens']==50
            assert result['runtime_cleanup']['tool_children_remaining']==0
        assert adapter.rpc.turns==2 and adapter.rpc.threads==2
        assert adapter.rpc.unloaded==2 and adapter.rpc.closed==0
        starts=[params for method,params in adapter.rpc.calls if method=='turn/start']
        assert starts[0]['threadId']!=starts[1]['threadId']
        assert starts[0]['input'][0]['text'].encode('utf-8')==prompt
        assert 'outputSchema' not in starts[0]  # Same model request policy as baseline exec.
    asyncio.run(run())


@pytest.mark.parametrize('mode,code',[('failed','EXECUTION_FAILED'),('no_final','INCOMPLETE_TURN'),
    ('wrong_turn','INVALID_TRACE'),('disconnect','APP_SERVER_DISCONNECTED'),('interactive','INTERACTIVE_REQUEST_DENIED')])
def test_no_false_success_and_no_model_replay(adapter,tmp_path,mode,code):
    adapter.rpc.mode=mode
    rpc=adapter.rpc
    root,job,_=prepared(tmp_path)
    async def run():
        with pytest.raises(JobError) as error:
            await adapter.run(job,root,lambda **v:None,lambda:False,adapter.redactor)
        assert error.value.code==code
        assert rpc.turns==1
        assert not (root/'control/final_response.raw.md').exists()
    asyncio.run(run())


def test_cancel_interrupts_turn_without_killing_healthy_daemon(adapter,tmp_path):
    adapter.rpc.mode='blocked'
    rpc=adapter.rpc
    root,job,_=prepared(tmp_path)
    async def run():
        with pytest.raises(JobError) as error:
            await adapter.run(job,root,lambda **v:None,lambda:rpc.turns>0,adapter.redactor)
        assert error.value.code=='CANCEL_REQUESTED'
        assert rpc.interrupts==1 and rpc.unloaded==1 and rpc.closed==0
        assert rpc.turns==1
    asyncio.run(run())


def test_timeout_waits_for_turn_cleanup(adapter,tmp_path):
    adapter.rpc.mode='blocked'
    rpc=adapter.rpc
    root,job,_=prepared(tmp_path)
    async def run():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.run(job,root,lambda **v:None,lambda:False,adapter.redactor),.1)
        assert rpc.interrupts==1 and rpc.unloaded==1
    asyncio.run(run())


def test_transport_never_auto_approves_server_requests(settings,tmp_path):
    root,_,_=prepared(tmp_path)
    rpc=AppServerRpc(settings,root,Redactor(['sensitive']))
    sent=[]
    async def write(message):
        sent.append(message)
    rpc._write=write
    async def run():
        await rpc._dispatch({'id':'request','method':'item/commandExecution/requestApproval','params':{'threadId':'one'}})
        await rpc._dispatch({'id':'escalation','method':'item/permissions/requestApproval','params':{'threadId':'one'}})
        await rpc._dispatch({'id':'token','method':'account/chatgptAuthTokens/refresh','params':{}})
        assert sent[0]['result']['decision']=='decline'
        assert all('error' in r for r in sent[1:])
    asyncio.run(run())


def test_transport_correlates_responses_and_redacts_errors(settings,tmp_path):
    root,_,_=prepared(tmp_path)
    rpc=AppServerRpc(settings,root,Redactor(['sensitive']))
    async def run():
        loop=asyncio.get_running_loop()
        f=loop.create_future();rpc.pending[5]=f
        await rpc._dispatch({'id':5,'result':{'ok':True}})
        assert (await f)=={'ok':True}
        f=loop.create_future();rpc.pending[6]=f
        await rpc._dispatch({'id':6,'error':{'code':-1,'message':'sensitive'}})
        with pytest.raises(AppServerRpcError) as e:
            await f
        assert 'sensitive' not in e.value.detail
    asyncio.run(run())


def test_json_jobs_protocol_unchanged(adapter,settings):
    from fastapi.testclient import TestClient
    from test_json_delivery import packet,submit
    from test_jobs import auth,wait
    import uuid
    with TestClient(create_app(settings,runner=adapter)) as client:
        assert client.get('/v1/ready',headers=auth(settings)).json()['execution_backend']=='codex-app-server'
        result=submit(client,settings,packet(),str(uuid.uuid4()))
        assert result.status_code==202,result.text
        final=wait(client,settings,result.json()['job_id'])
        assert final['status']=='completed',final
        artifact=next(x for x in final['artifacts'] if x['kind']=='translation')
        delivery=client.get(artifact['download_path'],headers=auth(settings)).json()
        assert delivery['rows'][0]['row_id']=='0'
        assert delivery['rows'][0]['target_text']=='模型译文 0'


def test_host_still_rejects_invalid_json(adapter,settings):
    from fastapi.testclient import TestClient
    from test_json_delivery import packet,submit
    from test_jobs import wait
    import uuid
    adapter.rpc.mode='bad_json'
    with TestClient(create_app(settings,runner=adapter)) as client:
        result=submit(client,settings,packet(),str(uuid.uuid4()))
        final=wait(client,settings,result.json()['job_id'])
        assert final['status']=='failed'
        assert final['error']['code']=='INVALID_JSON_RESULT'


def test_whole_task_tm_and_original_fields_still_preserved(adapter,settings):
    from test_translation_tasks import row,uploads,headers,wait_for_task
    import httpx
    async def run():
        data={'project_name':'Adapter','segments':[row(0,True),row(1),row(2,True)]}
        app=create_app(settings,runner=adapter)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(settings)) as client:
                accepted=await client.post('/v1/tasks',files=uploads(data),data={'options':'{}'})
                assert accepted.status_code==202,accepted.text
                final=await wait_for_task(client,accepted.json()['task_id'])
                assert final['status']=='completed',final
                delivered=(await client.get(final['result_path'])).json()
                assert delivered['segments'][0]['target_text']=='TM 原样译文'
                assert delivered['segments'][2]['target_text']=='TM 原样译文'
                for before,after in zip(data['segments'],delivered['segments']):
                    assert {k:v for k,v in before.items() if k!='target_text'}=={k:v for k,v in after.items() if k!='target_text'}
                assert adapter.rpc.turns==1
    asyncio.run(run())


@pytest.mark.windows
def test_real_windows_pipes_with_fixed_non_model_rpc_server(settings,tmp_path):
    from codex_remote_worker.windows import WindowsProcess
    fixture=tmp_path/'fixture_server.py'
    fixture.write_text('import sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if "id" in m: print(json.dumps({"id":m["id"],"result":{"platformFamily":"windows","ok":True,"echo":m.get("params",{}).get("echo")}}),flush=True)\n',encoding='utf-8')
    root,_,_=prepared(tmp_path)
    def factory(command,cwd,env,name):
        assert 'codex_app_server::outgoing_message=warn' in env['RUST_LOG']
        return WindowsProcess([str(settings.tool_python),str(fixture)],cwd,env,name)
    async def run():
        rpc=AppServerRpc(settings,root,Redactor(),process_factory=factory)
        try:
            result=await asyncio.wait_for(rpc.start(),8)
            assert result['ok']
            assert (await rpc.request('fixed/test',{},timeout=3))['ok']
            full_material='controller source text '*100000
            assert (await rpc.request('fixed/whole-project',{'echo':full_material},timeout=15))['echo']==full_material
        finally:
            await rpc.close()
    asyncio.run(run())


def test_full_project_tool_event_larger_than_one_mib_is_delivered(settings,tmp_path):
    import io
    from types import SimpleNamespace
    async def run():
        rpc=AppServerRpc(settings,tmp_path,Redactor())
        text='whole project material '*100000
        message={'method':'item/completed','params':{'threadId':'large-project','item':
            {'type':'dynamicToolCall','contentItems':[{'type':'inputText','text':text}]}}}
        rpc.process=SimpleNamespace(stdout=io.BytesIO(json.dumps(message).encode('utf-8')+b'\n'))
        async def direct(function,*args):return function(*args)
        rpc._blocking=direct
        queue=rpc.subscribe('large-project')
        await rpc._read_stdout()
        received=queue.get_nowait()
        assert received==message
        assert rpc.failure=='App Server stdout closed'  # EOF after the complete event, not a size failure.
    asyncio.run(run())


def test_core_routing_is_only_for_trusted_controller_not_model_tools(settings,tmp_path,monkeypatch):
    from codex_remote_worker.runner import runtime_environment
    monkeypatch.setenv('CODEX_WINDOWS_REGISTERED_CORE','1')
    monkeypatch.setenv('CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY','OpenAI.Codex_2p2nqsd0c76g0')
    root,_,_=prepared(tmp_path)
    controller=runtime_environment(settings,root)
    assert controller['CODEX_WINDOWS_REGISTERED_CORE']=='1'
    assert controller['CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY']=='OpenAI.Codex_2p2nqsd0c76g0'
    tools=thread_parameters(settings,root)['config']['shell_environment_policy.set']
    assert 'CODEX_WINDOWS_REGISTERED_CORE' not in tools
    assert 'CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY' not in tools
