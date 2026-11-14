from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid
from email.message import Message

from .common import (JobError, ROLES, canonical, contained_file, digest, file_digest,
                     is_link, name_key, now, safe_name, write_json, atomic_write)
from .config import Options, Settings
from .qa import read_source, check_csv
from .json_delivery import CONTRACT, JSON_CONTRACTS, load_json, check_json_result
from .tm_policy import PROJECT_CONTRACT


def ensure_space(settings: Settings):
    if shutil.disk_usage(settings.data_root).free < settings.min_free_bytes:
        raise JobError('STORAGE_FULL', '服务器磁盘余量不足。', 503)


def new_stage(settings: Settings):
    job_id = 'job_' + uuid.uuid4().hex
    root = settings.data_root / 'staging' / job_id
    for name in ['input', 'work', 'control', 'logs', 'artifacts']:
        (root / name).mkdir(parents=True, exist_ok=False)
    return job_id, root


async def receive_files(settings, uploads, stage):
    manifest, seen, total = {}, set(), 0
    for role in ROLES:
        upload = uploads[role]
        # Multipart parsers may silently strip a Windows drive/path from filename.
        header = Message()
        header['Content-Disposition'] = upload.headers.get('content-disposition', '')
        raw_name = header.get_filename() or ''
        if any(char in raw_name for char in '/\\:'):
            raise JobError('INVALID_FILENAME', '上传文件头包含路径，已拒绝。')
        filename = safe_name(upload.filename or '')
        key = name_key(filename)
        if key in seen:
            raise JobError('DUPLICATE_FILENAME', '上传资料存在同名文件。')
        seen.add(key)
        if Path(filename).suffix.lower() not in ROLES[role]:
            raise JobError('INVALID_FILE_TYPE', '上传文件类型不符合当前字段要求。')
        path = stage / 'input' / filename
        if os.name == 'nt' and len(str(path)) > 240:
            raise JobError('INVALID_FILENAME', '文件路径过长，请缩短文件名或保存根目录。')
        size = 0
        limit = min(settings.max_file_bytes, settings.max_prompt_bytes) if role == 'prompt_file' else settings.max_file_bytes
        with path.open('xb') as output:
            while data := await upload.read(64 * 1024):
                size += len(data)
                total += len(data)
                if size > limit or total > settings.max_upload_bytes:
                    raise JobError('UPLOAD_TOO_LARGE', '上传文件超过服务限制。', 413)
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if not size:
            raise JobError('EMPTY_FILE', '不接受空文件。')
        manifest[role] = {'filename': filename, 'bytes': size, 'sha256': file_digest(path)}
    prompt = (stage / 'input' / manifest['prompt_file']['filename']).read_bytes()
    try:
        if not prompt.decode('utf-8-sig').strip():
            raise ValueError('empty')
    except (UnicodeError, ValueError) as exc:
        raise JobError('INVALID_PROMPT', 'Prompt 必须为非空 UTF-8 文本。') from exc
    return manifest


def prepare_record(job_id, request_key, manifest, options: Options, stage: Path, *, codex_config_sha256=None):
    snapshot = read_source(stage / 'input' / manifest['source_file']['filename'])
    write_json(stage / 'control' / 'source.json', snapshot)
    for entry in manifest.values():
        shutil.copyfile(stage / 'input' / entry['filename'], stage / 'work' / entry['filename'])
    (stage / 'work' / 'outputs').mkdir()
    task = {'job_id': job_id, 'request_key': request_key,
            'fingerprint': digest(canonical({'files': manifest, 'options': options.model_dump()})),
            'status': 'queued', 'stage': 'queued', 'created_at': now(), 'updated_at': now(),
            'inputs': manifest, 'options': options.model_dump(), 'error': None,
            'codex_config_sha256': codex_config_sha256,
            'prompt_mode': 'uploaded_verbatim', 'prompt_sha256': manifest['prompt_file']['sha256'],
            'prompt_bytes': manifest['prompt_file']['bytes'],
            'output_filename': (Path(manifest['source_file']['filename']).stem +
                                ('_result.json' if snapshot.get('contract') in JSON_CONTRACTS else '_ZH-CN.csv')),
            'artifacts': [], 'progress': {'event_count': 0, 'last_activity_at': now()},
            'language_quality': 'not_independently_verified'}
    if snapshot.get('contract') == PROJECT_CONTRACT:
        task.update(project_name=snapshot['project_name'], request_id=snapshot['request_id'],
                    model_rows=len(snapshot['rows']), tm_reused_rows=len(snapshot['tm_reuse']))
    write_json(stage / 'control' / 'task.json', task)
    return task


def output_file(root: Path, filename: str):
    found = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = [name for name in dirs if not is_link(base / name)]
        for name in names:
            if name_key(name) == name_key(filename):
                found.append(contained_file(root.resolve(), base / name))
    if len(found) != 1:
        raise JobError('INVALID_ARTIFACT', '未找到唯一的目标 CSV；请查看最终回复和执行记录。')
    return found[0]


def verify_result(settings, job, root):
    for entry in job['inputs'].values():
        for directory in ['input', 'work']:
            path = contained_file((root / directory).resolve(), root / directory / entry['filename'])
            if file_digest(path) != entry['sha256']:
                raise JobError('INPUT_CHANGED', '任务输入原件或工作副本在运行期间被修改。')
    snapshot = json.loads((root / 'control' / 'source.json').read_text(encoding='utf-8'))
    if snapshot.get('contract') in JSON_CONTRACTS:
        raw = root / 'control' / 'final_response.raw.md'
        if not raw.exists():
            raise JobError('INVALID_JSON_RESULT', '模型没有提供最终 JSON 回复。')
        reply = contained_file((root / 'control').resolve(), raw)
        if reply.stat().st_size > settings.max_output_bytes:
            raise JobError('LIMIT_EXCEEDED', 'JSON 回复超过大小限制。')
        try:
            result = load_json(reply.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError, RecursionError) as exc:
            raise JobError('INVALID_JSON_RESULT', '最终回复不是有效的纯 JSON。') from exc
        report = check_json_result(snapshot, result)
        path = root / 'control' / job['output_filename']
        write_json(path, result)
    else:
        path = output_file(root / 'work', job['output_filename'])
        report = check_csv(snapshot, path)
    if path.stat().st_size > settings.max_output_bytes:
        raise JobError('LIMIT_EXCEEDED', '译文文件超过大小限制。')
    for secret in settings.secret_values():
        if secret.encode('utf-8') in path.read_bytes():
            raise JobError('INVALID_ARTIFACT', '译文含有服务凭据，已阻止发布。')
    write_json(root / 'control' / 'qa.json', report)
    return path, report


def publish(settings, job, root, redactor, translation=None):
    """Publish only known files after the process tree has stopped."""
    candidates = [
        ('preparation', root / 'control' / 'preparation.json', 'preparation.json'),
        ('preparation_stderr', root / 'logs' / 'preparation.stderr.log', 'preparation.stderr.log'),
        ('host_error', root / 'control' / 'host_error.json', 'host_error.json'),
        ('qa', root / 'control' / 'qa.json', 'qa.json'),
        ('trace', root / 'logs' / 'trace.jsonl', 'trace.jsonl'),
        ('stderr', root / 'logs' / 'stderr.log', 'stderr.log'),
        ('final_response', root / 'control' / 'final_response.raw.md',
         'final_response.json' if job['output_filename'].endswith('.json') else 'final_response.md'),
    ]
    if translation is not None:
        candidates.insert(0, ('translation', translation, job['output_filename']))
    artifacts = []
    for kind, source, filename in candidates:
        if not source.exists():
            continue
        contained_file(root.resolve(), source)
        maximum = settings.max_output_bytes if kind == 'translation' else settings.max_log_bytes
        if source.stat().st_size > maximum:
            raise JobError('LIMIT_EXCEEDED', '待发布文件超过大小限制。')
        data = source.read_bytes()
        if kind != 'translation':
            data = redactor.text(data.decode('utf-8', errors='replace')).encode('utf-8')
        target = root / 'artifacts' / filename
        atomic_write(target, data)
        artifacts.append(artifact(job['job_id'], kind, filename, data))
    # The run manifest contains other artifact hashes; its own hash is in the API manifest.
    run = {key: job.get(key) for key in [
        'job_id', 'status', 'stage', 'error', 'created_at', 'started_at', 'completed_at',
        'inputs', 'options', 'prompt_mode', 'prompt_sha256', 'prompt_bytes', 'execution', 'qa',
        'language_quality', 'progress', 'host_error', 'delivery_rechecks',
        'project_name', 'request_id', 'model_rows', 'tm_reused_rows']}
    run['artifacts'] = artifacts
    data = json.dumps(redactor.event(run), ensure_ascii=False, indent=2).encode('utf-8')
    atomic_write(root / 'artifacts' / 'run.json', data)
    artifacts.append(artifact(job['job_id'], 'run', 'run.json', data))
    return artifacts


def artifact(job_id, kind, filename, data):
    artifact_id = 'artifact_' + kind
    return {'id': artifact_id, 'kind': kind, 'filename': filename,
            'bytes': len(data), 'sha256': digest(data),
            'download_path': f'/v1/jobs/{job_id}/artifacts/{artifact_id}'}
