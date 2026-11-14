import json

from fastapi.testclient import TestClient

from codex_remote_worker.app import create_app


def test_access_audit_does_not_log_credentials_raw_paths_or_queries(settings, runner):
    secret = 'test-secret-must-not-appear'
    with TestClient(create_app(settings, runner=runner)) as client:
        assert client.get('/healthz?token=' + secret).status_code == 200
        assert client.get('/v1/ready', headers={'Authorization': 'Bearer ' + secret}).status_code == 401
        assert client.get('/' + secret).status_code == 404
        assert client.post('/v1/jobs', content=secret).status_code == 401
    text = (settings.data_root / 'http-access.jsonl').read_text(encoding='utf-8')
    assert secret not in text
    assert 'Authorization' not in text and 'token=' not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert [row['status'] for row in rows] == [200, 401, 404, 401]
    assert rows[2]['route'] == '<unmatched>'
    assert all('client_ip' in row and 'elapsed_ms' in row for row in rows)
