import json
import time

import pytest
from fastapi.testclient import TestClient

from codex_remote_worker import app_server_pool
from codex_remote_worker.app_server_service import build_app, trial_settings


def test_rejects_8788_data_before_loading_any_credentials(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'port': 8789, 'bind_host': '127.0.0.1',
                                'data_root': r'C:\CodexWorkerData-AppServer-8788',
                                'codex_home': r'C:\CodexWorkerData-AppServer-8788\private\codex-home'}))
    with pytest.raises(ValueError, match='own data directory'):
        trial_settings(path)


@pytest.mark.parametrize('signed_in', [False, True])
def test_http_status_and_model_start_follow_private_login(settings, monkeypatch, signed_in):
    settings.port = 8789
    if signed_in:
        (settings.codex_home / 'auth.json').write_text('{}')
    calls = []

    class Pool:
        blocked = None

        def __init__(self, settings):
            pass

        async def start(self):
            calls.append('start')

        async def close(self):
            calls.append('close')

        def ready(self):
            return {'ready': self.blocked is None, **(self.blocked or {})}

    monkeypatch.setattr(app_server_pool, 'ParallelTextAppServerRunner', Pool)
    with TestClient(build_app(settings)) as client:
        assert client.get('/healthz').json() == {'status': 'alive'}
        assert client.get('/v1/ready').status_code == 401
        headers = {'Authorization': 'Bearer ' + settings.service_token.get_secret_value()}
        response = client.get('/v1/ready', headers=headers)
        assert response.status_code == (200 if signed_in else 503)
        if not signed_in:
            assert response.json()['code'] == 'CHATGPT_LOGIN_REQUIRED'
            (settings.codex_home / 'auth.json').write_text('{}')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and client.get('/v1/ready', headers=headers).status_code == 503:
                time.sleep(0.2)
            assert client.get('/v1/ready', headers=headers).status_code == 200
    assert calls == ['start', 'close']
