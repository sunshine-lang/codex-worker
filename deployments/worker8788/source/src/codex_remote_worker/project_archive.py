"""Host-owned, hash-verified project archives. Execution directories never move."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from .common import JobError, ROLES, TERMINAL, atomic_write, contained_file, digest, is_link, safe_name
from .json_delivery import read_json_source, check_json_result

_ARCHIVE_LOCK = threading.RLock()


def io_path(path):
    """Do not require a machine-wide LongPathsEnabled registry change."""
    path = Path(path)
    if os.name == 'nt' and not str(path).startswith('\\\\?\\'):
        value = str(path.absolute())
        return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
    return path


def guard(base, path):
    """Inspect every existing component, including metadata files, before access."""
    base, path = Path(base), Path(path)
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        raise JobError('INVALID_PROJECT_PATH', '归档路径越出数据目录。') from None
    current = base
    for part in (None, *parts):
        if part is not None:
            if part in ('.', '..'):
                raise JobError('INVALID_PROJECT_PATH', '归档路径含无效部分。')
            current = current / part
        try:
            io_path(current).lstat()  # Includes dangling Windows junctions, not just live symlinks.
        except FileNotFoundError:
            continue
        if is_link(io_path(current)):
            raise JobError('INVALID_PROJECT_PATH', '归档路径、元数据和产物不允许链接或重解析点。')
    return path


def directory(base, path):
    path = guard(base, path)
    io_path(path).mkdir(parents=True, exist_ok=True)
    guard(base, path)
    return path


def save(base, path, value, *, raw=False):
    guard(base, path)
    data = value if raw else json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    atomic_write(io_path(path), data)
    guard(base, path)
    if digest(io_path(path).read_bytes()) != digest(data):
        raise JobError('ARCHIVE_HASH_MISMATCH', '归档写入后的哈希不一致。')


def project_directory(data_root, name):
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise JobError('INVALID_PROJECT', 'project_name 必须为不超过 200 字符的非空字符串。')
    base = Path(data_root)
    key = hashlib.sha256(name.encode('utf-8')).hexdigest()
    slug = re.sub(r'[^\w.-]', '-', name).strip(' .')[:24] or 'project'
    parent = directory(base, base / 'projects')
    path = directory(base, parent / ('p-' + slug + '-' + key[:16]))
    meta = guard(base, path / 'project.json')
    if io_path(meta).exists():
        value = json.loads(io_path(meta).read_text(encoding='utf-8'))
        if value.get('project_name') != name or value.get('project_key') != key:
            raise JobError('PROJECT_CONFLICT', '项目完整哈希或原始名称不一致。')
    else:
        save(base, meta, {'project_name': name, 'project_key': key})
    return path


def archive_job(settings, job, root):
    with _ARCHIVE_LOCK:
        return _archive_job(settings, job, root)


def verified_bytes(root, path, entry):
    source = contained_file(root.resolve(), path)
    data = source.read_bytes()
    if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
        raise JobError('ARCHIVE_SOURCE_CHANGED', '归档源文件大小或哈希发生变化。')
    return data


def _archive_job(settings, job, root):
    if not job.get('project_name'):
        return None
    base, root = Path(settings.data_root), Path(root)
    project = project_directory(base, job['project_name'])
    if not re.fullmatch(r'job_[a-f0-9]{32}', job['job_id']):
        raise JobError('INVALID_JOB_ID', '任务编号无效。')
    jobs = directory(base, project / 'jobs')
    target = directory(base, jobs / job['job_id'])
    relative = target.relative_to(base).as_posix()
    meta = guard(base, target / 'job.json')
    if io_path(meta).exists():
        previous = json.loads(io_path(meta).read_text(encoding='utf-8'))
        if previous.get('status') in TERMINAL and job.get('status') not in TERMINAL:
            return relative  # A late submission-registration callback cannot erase terminal state.
    inputs = directory(base, target / 'input')
    for role, entry in job['inputs'].items():
        if role not in ROLES:
            raise JobError('INVALID_PROJECT_INPUT', '未知的归档输入角色。')
        source = root / 'input' / entry['filename']
        save(base, inputs / (role + source.suffix.lower()), verified_bytes(root, source, entry), raw=True)
    artifacts = directory(base, target / 'artifacts')
    for entry in job.get('artifacts', []):
        filename = safe_name(entry['filename'])
        save(base, artifacts / filename, verified_bytes(root, root / 'artifacts' / filename, entry), raw=True)
    source = contained_file(root.resolve(), root / 'input' / job['inputs']['source_file']['filename'])
    packet = read_json_source(source)
    if packet.get('contract') != 'siemens-json/2' or packet['project_name'] != job['project_name'] or packet['request_id'] != job['request_id']:
        raise JobError('INVALID_PROJECT_INPUT', '项目归档元数据与输入不一致。')
    save(base, target / 'tm-reuse.json', {'project_name': job['project_name'], 'request_id': job['request_id'], 'rows': packet['tm_reuse']})
    values = {row['row_id']: row['record']['target_text'] for row in packet['tm_reuse']}
    if packet['phase'] == 'review':
        values.update({row['row_id']: row['record']['target_text'] for row in packet['rows']})
    else:
        delivery = next((a for a in job.get('artifacts', []) if a['kind'] == 'translation'), None)
        if delivery:
            result = json.loads(verified_bytes(root, root / 'artifacts' / delivery['filename'], delivery))
            check_json_result(packet, result)
            values.update({row['row_id']: row['target_text'] for row in result['rows']})
    all_rows = sorted(packet['rows'] + packet['tm_reuse'], key=lambda row: (len(row['row_id']), row['row_id']))
    if len(values) == len(all_rows) and all(isinstance(v, str) and v.strip() for v in values.values()):
        segments = []
        for row in all_rows:
            record = copy.deepcopy(row['record'])
            record['target_text'] = values[row['row_id']]
            segments.append(record)
        save(base, target / 'batch-candidates.json', {'project_name': job['project_name'], 'segments': segments})
    save(base, meta, {**{k: job.get(k) for k in ('job_id', 'project_name', 'request_id', 'status', 'stage',
        'created_at', 'started_at', 'completed_at', 'inputs', 'artifacts', 'error', 'model_rows', 'tm_reused_rows')},
        'project_archive': relative, 'archive_error': None})
    return relative
