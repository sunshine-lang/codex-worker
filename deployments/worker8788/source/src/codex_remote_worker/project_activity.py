"""Small, public descriptions of completed project operations."""
from __future__ import annotations

ACTION_WINDOW = 4


def describe_segments(event, indices, records, noun):
    count = len(indices)
    event['segment_count'] = count
    if count <= 8:
        identifiers = [records[i].get('segment_id') for i in indices]
        if all(type(v) in (str, int) and len(str(v)) <= 128 for v in identifiers):
            event['segment_ids'] = identifiers
    if count >= 3 and indices[-1] - indices[0] == count - 1 and all(
            n == indices[0] + j for j, n in enumerate(indices)):
        return f'第 {indices[0] + 1}–{indices[-1] + 1} 段{noun}'
    if 0 < count <= 4:
        numbers = '、'.join(str(i + 1) for i in indices)
        return f'第 {numbers} 段{noun}'
    return f'{count} 段{noun}'


def completed_action(session, tool, args, result, at, revisions_before):
    event = {'seq': len(session.calls) + 1, 'at': at, 'actor': 'agent', 'tool': tool}
    if tool == 'project_read':
        op = args['operation']
        if op == 'guides':
            role = {'prompt': 'prompt_file', 'project_style': 'project_style',
                    'general_style': 'general_style'}[args['query']]
            event['document_role'] = role
            if role == 'prompt_file':
                event['file_name'] = 'prompt_file.md'  # The session reads this staged file.
                message = '已读取 prompt_file.md'
            else:
                name = session.task.get('inputs', {}).get(role, {}).get('filename')
                message = '已查阅项目风格指南' if role == 'project_style' else '已查阅通用风格指南'
                if name:
                    event['file_name'] = name
                    message = f'已查阅 {name} 的内容'
            action = 'read_document'
        elif op == 'source':
            start = args['start']
            end = min(len(session.records), start + args['count']) if args['count'] else len(session.records)
            indices = range(start, end)
            action = 'read_source'
            if indices:
                message = '已读取' + describe_segments(event, indices, session.records, '原文')
            else:
                message = '已查询原文，所选范围为空'
        else:
            action, message = {
                'overview': ('read_overview', '已查看项目概况'),
                'search': ('search_project', '已检索订单原文和译文'),
                'checks': ('read_checks', '已查看本单的规则检查结果'),
            }[op]
        if args.get('char_offset') or result.get('has_more'):
            event['paged'] = True
            message += '（分页）'
    elif tool == 'project_write':
        indices = [int(k) for k in result['saved_row_ids']]
        event['checkpoint_file'] = 'project-session-state.json'
        if not indices:
            action, message = 'save_notes', '已保存项目备注'
        else:
            target = describe_segments(event, indices, session.records, '译文')
            revised = session.revisions - revisions_before
            if revised == len(indices):
                action, message = 'revise_translation', '已修订' + target
            else:
                action, message = 'save_translation', '已保存' + target
                if revised:
                    message += f'，其中 {revised} 段有修订'
    elif tool == 'project_review':
        indices = list(dict.fromkeys(int(k) for k in args['row_ids']))
        if indices:
            describe_segments(event, indices, session.records, '译文')
            action, message = 'self_check', f'已完成 {len(indices)} 段译文的 Worker 自检'
        else:
            action, message = 'save_review_note', '已保存 Worker 自检备注'
    else:
        raise ValueError('Unknown completed project tool')
    return {**event, 'action': action, 'message': message}


def activity_progress(task):
    progress = task.get('session_progress')
    actions = list(task.get('recent_actions') or [])
    if task['status'] == 'completed' and task.get('completed_at') and task.get('result_sha256'):
        # _finish stores this timestamp and digest only after writing this task's result.json.
        seq = max((progress or {}).get('tool_calls', 0), actions[-1]['seq'] if actions else 0) + 1
        actions.append({'seq': seq, 'at': task['completed_at'], 'actor': 'worker',
                        'action': 'create_result', 'message': '已创建结果文件 result.json', 'file_name': 'result.json'})
    if progress is None and not actions:
        return None
    return {**(progress or {}), 'recent_actions': actions[-ACTION_WINDOW:]}
