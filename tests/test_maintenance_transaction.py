import copy

import pytest

from codex_remote_worker.common import JobError
from codex_remote_worker.store import Store


def test_maintenance_blocks_new_accept_and_claim_but_preserves_idempotent_results(tmp_path):
    store = Store(tmp_path / 'jobs.sqlite3')
    original = {'job_id': 'job_one', 'request_key': 'key-one', 'fingerprint': 'one',
                'status': 'queued', 'created_at': '2026-11-11T00:00:00Z'}
    stage = tmp_path / 'stage-one'
    stage.mkdir()
    target = tmp_path / 'job-one'
    try:
        store.accept(original, stage, target, 10)
        marker = tmp_path / '.maintenance.json'
        marker.write_text('{}')
        assert store.claim() is None
        assert store.accept(original, stage, target, 10)[1] is True
        second_stage = tmp_path / 'stage-two'
        second_stage.mkdir()
        second = {**copy.deepcopy(original), 'job_id': 'job_two', 'request_key': 'key-two', 'fingerprint': 'two'}
        with pytest.raises(JobError) as error:
            store.accept(second, second_stage, tmp_path / 'job-two', 10)
        assert error.value.code == 'MAINTENANCE'
        assert second_stage.exists() and not (tmp_path / 'job-two').exists()
        marker.unlink()
        assert store.claim()['job_id'] == 'job_one'
    finally:
        store.close()
