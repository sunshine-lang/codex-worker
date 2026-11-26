"""Add repair controls to the three existing 8788 MVP-v2 nodes without changing their bindings."""


def once(code, before, after):
    if code.count(before) != 1:
        raise ValueError('The 8788 node no longer matches its verified source: ' + before[:65])
    return code.replace(before, after, 1)


def change(i, code):
    if i == 1:
        code = once(code, '    submission_key = None', '''    submission_key = None
    replaces_task_id = None
    if isinstance(original, dict) and original.get('operation') == 'replace':
        replaces_task_id = original.get('task_id')
        if not isinstance(replaces_task_id, str) or not re.fullmatch(r'task_[a-f0-9]{32}', replaces_task_id):
            raise ValueError('换稿需要原订单的 task_id。')
        try:
            submission_key = str(uuid.UUID(original['submission_key']))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('请为本次换稿提供 submission_key（UUID），重试沿用同一个值。') from None
        original = {k:v for k,v in original.items() if k not in ('operation','task_id','submission_key')}
    if isinstance(original, dict) and original.get('operation') == 'resume':
        task_id = original.get('task_id')
        if not isinstance(task_id, str) or not re.fullmatch(r'task_[a-f0-9]{32}', task_id) or 'segments' in original:
            raise ValueError('原稿恢复只需要原任务号和 recovery_key。')
        try:
            key = str(uuid.UUID(original['recovery_key']))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('请为本次恢复提供 recovery_key（UUID），重试沿用同一个值。') from None
        return {'contract':'translation-mvp/background-1','mode':'worker_tasks','operation':'resume',
                'task_id':task_id, 'recovery_key':key, 'worker_contacted':False}
''')
        code = once(code, "'submission_key': submission_key,", "'submission_key': submission_key, 'replaces_task_id':replaces_task_id,")
        return once(code, "            if key:\n                try:state['submission_key']", """            if key and state.get('replaces_task_id') and key.lower() != state['submission_key']:
                raise ValueError('换稿 JSON 和节点指定了不同的提交编号。')
            if key:
                try:state['submission_key']""")
    if i == 2:
        code = once(code, 'timeout_seconds=1800, *, tm_policy=None, language_mode=None):', 'timeout_seconds=1800, *, tm_policy=None, language_mode=None, replaces_task_id=None):')
        code = once(code, "        options = {'timeout_seconds': timeout_seconds}", """        options = {'timeout_seconds': timeout_seconds}
        if replaces_task_id:
            options['replaces_task_id'] = replaces_task_id""")
        code = once(code, "            snapshot_started=time.monotonic()", """            if state.get('replaces_task_id') and state['worker_capabilities'].get('replace_in_place') is not True:
                raise ValueError('WORKER_UPGRADE_REQUIRED: 此 Worker 尚未支持原位换稿。')
            snapshot_started=time.monotonic()""")
        code = once(code, 'frozen, state[\'submission_key\'], timeout, tm_policy=chosen_tm_policy, language_mode=chosen_language_mode)',
                    "frozen, state['submission_key'], timeout, tm_policy=chosen_tm_policy, language_mode=chosen_language_mode, replaces_task_id=state.get('replaces_task_id'))")
        code = once(code, "if value.get('status') not in ('queued','running','completed','failed')", "if value.get('status') not in ('queued','running','stopping','stopped','completed','failed')")
        return once(code, 'if state.get("operation") == "stop":\n            return Data(data={"operation":"stop"', 'if state.get("operation") in ("stop", "resume"):\n            return Data(data={"operation":state["operation"]')
    if i == 3:
        guard = next(line.strip().split(' or ')[0][3:] for line in code.splitlines() if 'if client.base != ' in line and 'or not re.fullmatch' in line)
        return once(code, '    if operation == "stop":', '''    if operation == "resume":
        if '''+guard+''' or not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
            raise ValueError("请提供此 8788 翻译服务的任务编号。")
        capabilities = await project_request(client.capabilities)
        if capabilities.get("task_resume") is not True:
            raise ValueError("WORKER_UPGRADE_REQUIRED: 此 Worker 尚未支持原位恢复。")
        value = await project_request(lambda: client.request("POST", "/v1/tasks/"+task_id+"/resume",
                                      headers={"Idempotency-Key":state["recovery_key"]}, timeout=120))
        if value.get("contract") != "translation-task/1" or value.get("task_id") != task_id:
            raise ValueError("WORKER_RESPONSE_INVALID: 恢复回执与原单不符。")
        return worker_view(value)
    if operation == "stop":''')
    raise ValueError(i)
