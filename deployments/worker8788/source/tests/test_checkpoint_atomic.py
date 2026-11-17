import os

import pytest

from codex_remote_worker import common


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing failures')
def test_checkpoint_retries_transient_windows_sharing_without_losing_original(tmp_path,monkeypatch):
    target=tmp_path/'state.json';target.write_bytes(b'old')
    replace=common.os.replace;attempts=[]
    def sharing(source,destination):
        attempts.append(1)
        if len(attempts)<3:
            assert target.read_bytes()==b'old'
            error=PermissionError('temporarily shared');error.winerror=32
            raise error
        return replace(source,destination)
    monkeypatch.setattr(common.os,'replace',sharing)
    monkeypatch.setattr(common.time,'sleep',lambda _:None)
    common.atomic_write(target,b'new')
    assert len(attempts)==3 and target.read_bytes()==b'new'
    assert list(tmp_path.iterdir())==[target]


@pytest.mark.skipif(os.name!='nt',reason='Windows permission failures')
def test_persistent_checkpoint_denial_is_bounded_and_preserves_original(tmp_path,monkeypatch):
    target=tmp_path/'state.json';target.write_bytes(b'old')
    attempts=[]
    def denied(*args):
        attempts.append(1)
        error=PermissionError('denied');error.winerror=5
        raise error
    monkeypatch.setattr(common.os,'replace',denied)
    monkeypatch.setattr(common.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError):common.atomic_write(target,b'new')
    assert len(attempts)==5 and target.read_bytes()==b'old'
    assert list(tmp_path.iterdir())==[target]
