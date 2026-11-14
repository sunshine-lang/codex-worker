"""Remove a bounded date range from active storage, retaining a recoverable copy."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

DATA = Path(r'C:\CodexWorkerData')
ACTIVE = ('queued', 'running', 'checking', 'cancelling')


def guarded(path, base):
    path, base = path.absolute(), base.absolute()
    assert path != base and path.is_relative_to(base), 'Path is outside intended storage'
    current = base
    for part in (None, *path.relative_to(base).parts):
        if part is not None:
            current /= part
        if current.exists() or current.is_symlink():
            assert not current.is_symlink() and not current.is_junction(), 'Reparse-point storage path rejected'
    assert path.resolve().is_relative_to(base.resolve()), 'Resolved target is outside intended storage'
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--expected-count', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--operation-id')
    args = parser.parse_args()
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.since) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.until)
    assert args.since < args.until
    if args.apply:
        marker = json.loads((DATA / '.maintenance.json').read_text(encoding='utf-8-sig'))
        assert args.operation_id and marker['operation_id'] == args.operation_id, 'Maintenance ownership changed'
    db = sqlite3.connect((DATA/'jobs.sqlite3').as_uri() + ('?mode=rw' if args.apply else '?mode=ro'),
                         uri=True, isolation_level=None, timeout=10)
    moved = []
    committed = False
    backup = None
    try:
        db.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
        assert not db.execute("select 1 from jobs where status in ('queued','running','checking','cancelling') limit 1").fetchone(), 'Active jobs; nothing cleared'
        columns = [row[1] for row in db.execute('pragma table_info(jobs)')]
        rows = db.execute('select * from jobs where created_at >= ? and created_at < ? order by created_at',
                          (args.since, args.until)).fetchall()
        assert len(rows) == args.expected_count, 'Job range changed; nothing cleared'
        records = [dict(zip(columns, row)) for row in rows]
        jobs = [json.loads(row['record']) for row in records]
        plans = []
        for job in jobs:
            job_id = job['job_id']
            assert re.fullmatch(r'job_[a-f0-9]{32}', job_id) and job['status'] not in ACTIVE
            source = guarded(DATA/'jobs'/job_id, DATA/'jobs')
            assert source.is_dir(), 'Expected job directory missing'
            plans.append((source, Path('jobs')/job_id))
            if job.get('project_archive'):
                relative = Path(job['project_archive'])
                assert not relative.is_absolute() and '..' not in relative.parts
                assert relative.parts[0] == 'projects' and relative.name == job_id and relative.parent.name == 'jobs'
                archive = guarded(DATA/relative, DATA/'projects')
                assert archive.is_dir(), 'Expected project archive missing'
                plans.append((archive, relative))
        assert len({str(p[0]) for p in plans}) == len(plans)
        summary = {'since_utc': args.since, 'until_exclusive_utc': args.until, 'jobs': len(jobs),
                   'by_day': dict(Counter(j['created_at'][:10] for j in jobs)),
                   'directories': len(plans), 'job_ids': [j['job_id'] for j in jobs]}
        if not args.apply:
            print(json.dumps({'dry_run': True, **summary}, ensure_ascii=False))
            return
        untouched = db.execute('select * from jobs where created_at < ? or created_at >= ? order by job_id',
                               (args.since, args.until)).fetchall()
        backup = guarded(DATA/'maintenance-backups'/('job-cleanup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')), DATA)
        backup.mkdir(parents=True, exist_ok=False)
        manifest = {'operation_id': args.operation_id, **summary, 'columns': columns, 'records': records,
                    'moves': [{'source': str(source), 'backup': str(backup/relative)} for source, relative in plans],
                    'phase': 'prepared'}
        manifest_path = backup/'cleanup.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        for source, relative in plans:
            destination = guarded(backup/relative, backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            assert not destination.exists()
            source.rename(destination)
            moved.append((source, destination))
        for job in jobs:
            cursor = db.execute('delete from jobs where job_id=? and created_at >= ? and created_at < ?',
                                (job['job_id'], args.since, args.until))
            assert cursor.rowcount == 1
        assert db.execute('select * from jobs order by job_id').fetchall() == untouched, 'Unrelated records changed'
        assert all(not source.exists() and destination.is_dir() for source, destination in moved)
        manifest['phase'] = 'files_moved_database_pending'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        db.commit()
        committed = True
        manifest.update(phase='completed', remaining_jobs=len(untouched),
                        unaffected_history_sha256=hashlib.sha256(json.dumps(untouched, ensure_ascii=False).encode()).hexdigest())
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'cleared': True, **summary, 'remaining_jobs': len(untouched), 'backup': str(backup)}, ensure_ascii=False))
    except BaseException:
        if not committed:
            db.rollback()
            for source, destination in reversed(moved):
                guarded(source, DATA)
                guarded(destination, backup)
                assert not source.exists() and destination.exists()
                destination.rename(source)
        raise
    finally:
        if not committed:
            db.rollback()
        db.close()


if __name__ == '__main__':
    main()
