"""Explicit, checkpoint-verified recovery of an idle continuous project (no model call here)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from .common import JobError, TERMINAL, file_digest, now, write_json
from .project_session import ProjectSession
from .runner import codex_config_digest


def queue_resume(settings, task_id):
    if not re.fullmatch(r'task_[0-9a-f]{32}', task_id):
        raise JobError('INVALID_TASK_ID', '任务编号格式无效。')
    data = settings.data_root.resolve(strict=True)
    marker = data/'.maintenance.json'
    operation = uuid.uuid4().hex
    with marker.open('x', encoding='utf-8') as stream:
        json.dump({'operation':operation, 'reason':'explicit checkpoint resume', 'task_id':task_id}, stream)
    committed = False
    verified = False
    db = None
    try:
        db = sqlite3.connect(data/'jobs.sqlite3', timeout=10, isolation_level=None)
        db.execute('ATTACH DATABASE ? AS tasks_db', (str(data/'tasks.sqlite3'),))
        db.execute('BEGIN IMMEDIATE')
        for table in ('jobs', 'tasks_db.tasks'):
            terminal = TERMINAL | {'stopped'} if table == 'tasks_db.tasks' else TERMINAL
            if any(status not in terminal for (status,) in db.execute('SELECT DISTINCT status FROM '+table)):
                raise JobError('WORKER_BUSY', '存在未结束任务，请等空闲后恢复。')
        row = db.execute('SELECT record FROM tasks_db.tasks WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise JobError('TASK_NOT_FOUND', '任务不存在。')
        task = json.loads(row[0])
        job_id = task.get('session_job_id', '')
        if not task.get('continuous_project_session') or not re.fullmatch(r'job_[0-9a-f]{32}', job_id):
            raise JobError('INVALID_PROJECT_CHECKPOINT', '此任务不是可恢复的整单会话。')
        job_row = db.execute('SELECT record FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        job = json.loads(job_row[0]) if job_row else {}
        execution = job.get('execution') or {}
        prior_resume = job.get('project_resume') or {}
        # After an environmentless checkpoint rehydration, the next recovery
        # belongs to its new thread. Accept only the explicitly recorded lineage.
        prior_thread = prior_resume.get('thread_id')
        if (execution.get('thread_recovery_mode') == 'checkpoint_rehydrate'
                and execution.get('checkpoint_source_thread_id') == prior_thread
                and execution.get('thread_id')):
            prior_thread = execution['thread_id']
        thread_ids = {v for v in (execution.get('thread_id'), task.get('codex_thread_id'), prior_thread) if v}
        thread_id = next(iter(thread_ids)) if len(thread_ids)==1 else None
        if (task['status'] not in ('failed','interrupted') or job.get('status') not in ('failed','interrupted')
                or job.get('request_id') != task_id or not thread_id
                or (prior_resume and (prior_resume.get('job_id') != job_id or prior_resume.get('task_id') != task_id))):
            raise JobError('INVALID_PROJECT_CHECKPOINT', '只能显式恢复绑定一致的失败或中断会话。')
        if (execution.get('runtime_cleanup') or {}).get('tool_children_remaining') != 0:
            raise JobError('PROCESS_CLEANUP_FAILED', '旧执行进程未确认清理，不能恢复。')
        if codex_config_digest(settings) != job.get('codex_config_sha256'):
            raise JobError('CODEX_CONFIG_CHANGED', '模型配置与原任务不同，未重排任务。')
        root = data/'jobs'/job_id
        for owner, directory in ((job, root), (task, data/'tasks'/task_id)):
            for entry in owner['inputs'].values():
                path = (directory/'input'/entry['filename']).resolve(strict=True)
                if not path.is_relative_to((directory/'input').resolve()) or file_digest(path) != entry['sha256']:
                    raise JobError('INPUT_CHANGED', '原始资料与提交记录不同，未恢复。')
        session = ProjectSession(settings, root)
        if not session.restored or session.phase not in ('translating','reviewing'):
            raise JobError('INVALID_PROJECT_CHECKPOINT', '没有可续翻的草稿检查点。')
        checkpoint = root/'control/project-session-state.json'
        checkpoint_sha = file_digest(checkpoint)
        backup = data/'maintenance-backups'/('project-resume-'+operation)
        backup.mkdir(parents=True)
        write_json(backup/'job-record.json', job)
        write_json(backup/'task-record.json', task)
        for name in ('control','logs','artifacts'):
            if (root/name).is_dir():
                shutil.copytree(root/name, backup/name)
        if file_digest(checkpoint) != checkpoint_sha or file_digest(backup/'control/project-session-state.json') != checkpoint_sha:
            raise JobError('INPUT_CHANGED', '备份期间检查点变化，未恢复。')
        stamp = now()
        job.setdefault('execution_history', []).append({'execution':execution, 'error':job.get('error'),
            'completed_at':job.get('completed_at'), 'backup':str(backup)})
        job.update(status='queued', stage='queued', started_at=None, completed_at=None, updated_at=stamp,
            error=None, qa=None, artifacts=[], process=None,
            project_resume={'task_id':task_id, 'job_id':job_id, 'thread_id':thread_id,
                            'checkpoint_sha256':checkpoint_sha, 'requested_at':stamp, 'backup':str(backup)},
            execution={**execution, 'project_session':session.progress(), 'turn_completed':False, 'exit_code':None})
        task.update(status='queued', stage='waiting_for_worker', error=None, updated_at=stamp,
            codex_thread_id=thread_id,
            translated_rows=len(session.values), session_progress=session.progress(),
            self_review_status='running' if session.phase=='reviewing' else 'not_started')
        for batch in task['batches']:
            batch.update(status='queued')
        db.execute('UPDATE jobs SET status=?,record=? WHERE job_id=?', ('queued',json.dumps(job),job_id))
        db.execute('UPDATE tasks_db.tasks SET status=?,record=? WHERE task_id=?', ('queued',json.dumps(task),task_id))
        db.execute('COMMIT')
        committed = True
        verified = (db.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='queued'
                    and db.execute('SELECT status FROM tasks_db.tasks WHERE task_id=?',(task_id,)).fetchone()[0]=='queued')
        if not verified:
            raise RuntimeError('Resume state verification failed; maintenance marker retained.')
        result = {'task_id':task_id, 'job_id':job_id, 'thread_id':thread_id,
                  'restored_rows':len(session.values), 'first_untranslated_row_id':session.overview()['first_untranslated_row_id'],
                  'backup':str(backup), 'status':'queued', 'model_called_by_recovery':False}
        write_json(backup/'resume.json', result)
        return result
    finally:
        if db is not None:
            if db.in_transaction:
                db.execute('ROLLBACK')
            db.close()
        if not committed or verified:
            if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('operation')==operation:
                marker.unlink()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--task-id',required=True)
    args=parser.parse_args()
    from .app_server_service import trial_settings
    result=queue_resume(trial_settings(args.config),args.task_id)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
