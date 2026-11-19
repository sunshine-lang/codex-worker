"""Upgrade only the IM development graph; dry run unless --apply is supplied."""
import argparse
import copy
import hashlib
import json
import os
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BASE = 'http://218.60.7.98:7860'
DEV = '9f26a456-0a79-4b62-9cfe-438006c339b3'
PROD = '0da8b7c3-dc59-4df7-85cc-5f37cc84158b'
PROJECT = '47bc7ec5-e7bb-4304-8b0c-19298c6ace05'
WORKER = 'http://52.62.69.192:8789'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def flow(flow_id, graph=None, revision=None):
    headers = {'x-api-key': os.environ['LANGFLOW_API_KEY']}
    body = None
    if graph is not None:
        if flow_id != DEV or not revision:
            raise ValueError('Expected versioned development flow')
        headers.update({'Content-Type':'application/json', 'If-Match':revision})
        body = json.dumps({'data':graph}, ensure_ascii=False).encode()
    req = Request(f'{BASE}/api/v1/flows/{flow_id}', data=body, headers=headers,
                  method='GET' if body is None else 'PATCH')
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=60) as response:
        value = json.load(response)
        return value, response.headers.get('ETag') or value.get('version_token')


def replace_once(source, before, after):
    if after in source:
        return source
    if source.count(before) != 1:
        raise ValueError('Unexpected node implementation; inspect current dev graph')
    return source.replace(before, after)


def patch_graph(original):
    graph = copy.deepcopy(original)
    templates = {node['id']:node['data']['node'] for node in graph['nodes']}
    for name in ('MVPInputs','MVPSubmit','MVPQuery'):
        node = templates['CustomComponent-'+name]
        template = node['template']
        source = template['code']['value']
        if name == 'MVPInputs':
            source = replace_once(source, "original.get('operation') in ('status','result','process',",
                                  "original.get('operation') in ('stop','status','result','process',")
        else:
            if template['service_url']['value'].rstrip('/') != WORKER:
                raise ValueError('Development Worker endpoint mismatch')
            source = replace_once(source,
                "value.get('task_id') != task_id or value.get('status') not in ('queued','running','completed','failed')",
                "value.get('task_id') != task_id or value.get('status') not in ('queued','running','stopping','stopped','completed','failed')")
            source = replace_once(source, 'while remote["status"] in ("queued", "running"):',
                                  'while remote["status"] in ("queued", "running", "stopping"):')
            if name == 'MVPSubmit':
                anchor = '        if state.get("operation") in ("status", "result", "process", "diagnostics", "handoff_diagnostics"):'
                source = replace_once(source, anchor, '''        if state.get("operation") == "stop":
            return Data(data={"operation":"stop", "public_snapshot":{
                "status":"query_not_requested", "task_id":state["task_id"], "query_only":True}})
'''+anchor)
            else:
                anchor = '    operation, task_id = state["operation"], state["task_id"]\n'
                source = replace_once(source, anchor, anchor+'''    if operation == "stop":
        if client.base != "http://52.62.69.192:8789" or not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
            raise ValueError("INVALID_STOP_TARGET: 请提供开发 Worker 的任务编号。")
        capabilities = await project_request(client.capabilities)
        if capabilities.get("task_stop") is not True:
            return {"operation":"stop", "task_id":task_id, "status":"worker_upgrade_required",
                    "result_available":False, "error":{"code":"WORKER_UPGRADE_REQUIRED",
                    "message":"8789 尚未启用整单停止，请先更新开发 Worker。"}}
        value = await project_request(lambda: client.request("POST", "/v1/tasks/"+task_id+"/stop", timeout=30))
        if (value.get("contract") != "translation-task/1" or value.get("task_id") != task_id
                or value.get("status") not in ("stopping","stopped","completed","failed")):
            raise ValueError("WORKER_RESPONSE_INVALID: 停止回执与本单不匹配。")
        return worker_view(value)
''')
        compile(source, name, 'exec')
        if template['code']['value'] != source:
            template['code']['value'] = source
            node.setdefault('metadata', {})['code_hash'] = hashlib.sha256(source.encode()).hexdigest()[:12]
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    dev, revision = flow(DEV)
    if dev['name'] != '翻译流程MVP-v2-dev' or dev['folder_id'] != PROJECT:
        raise ValueError('Unexpected development flow')
    graph = patch_graph(dev['data'])
    if graph != dev['data'] and args.apply:
        prod, _ = flow(PROD)
        flow(DEV, graph, revision)
        if flow(DEV)[0]['data'] != graph or flow(PROD)[0]['data'] != prod['data']:
            raise RuntimeError('Flow readback mismatch')
    print(json.dumps({'flow':DEV, 'updated':args.apply and dev['data'] != graph,
                      'worker':WORKER, 'nodes':len(graph['nodes']), 'edges':len(graph['edges'])}))


if __name__ == '__main__':
    main()
