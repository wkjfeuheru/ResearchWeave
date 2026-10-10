"""ContentStore contract tests, including S3 conditional writes and exact hash validation."""

import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from researchx.storage.content import S3ContentConfig, S3ContentStore, content_store


class ClientError(Exception):
    def __init__(self, status):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


class S3:
    def __init__(self):
        self.objects, self.requests = {}, []
        self.closed = 0

    def put_object(self, **kwargs):
        self.requests.append(kwargs)
        assert kwargs["IfNoneMatch"] == "*"
        key = kwargs["Key"]
        if key in self.objects:
            raise ClientError(412)
        self.objects[key] = kwargs["Body"]

    def get_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError(404)
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError(404)
        return {}

    def close(self):
        self.closed += 1


async def test_s3_conditional_put_replay_integrity_and_scope(monkeypatch):
    fake = S3()
    monkeypatch.setitem(sys.modules, "botocore.exceptions", SimpleNamespace(ClientError=ClientError))
    store = S3ContentStore(S3ContentConfig("http://minio:9000", "private-research"))
    monkeypatch.setattr(store, "_client", lambda: fake)
    workspace = "a" * 16
    reference = await store.put(workspace, "不可变正文".encode())
    assert await store.exists(reference)
    assert await store.read(reference) == "不可变正文".encode()
    assert await store.put(workspace, "不可变正文".encode()) == reference
    assert len(fake.objects) == 1
    assert reference.content_hash == hashlib.sha256("不可变正文".encode()).hexdigest()
    assert fake.closed == 6  # PUT/GET, HEAD, GET, duplicate PUT/GET.
    assert await store.exists_many([reference])
    assert fake.closed == 7


async def test_s3_corruption_and_missing_objects_are_not_silently_replaced(monkeypatch):
    fake = S3()
    monkeypatch.setitem(sys.modules, "botocore.exceptions", SimpleNamespace(ClientError=ClientError))
    store = S3ContentStore(S3ContentConfig("https://example.invalid", "private-research"))
    monkeypatch.setattr(store, "_client", lambda: fake)
    reference = await store.put("b" * 16, b"original")
    key = next(iter(fake.objects))
    fake.objects[key] = b"tampered"
    with pytest.raises(ValueError, match="hash"):
        await store.read(reference)
    with pytest.raises(ValueError, match="hash"):
        await store.put("b" * 16, b"original")
    assert fake.objects[key] == b"tampered"
    fake.objects.clear()
    assert not await store.exists(reference)
    with pytest.raises(OSError, match="read failed"):
        await store.read(reference)


def test_backend_configuration_is_explicit(monkeypatch):
    monkeypatch.setenv("RESEARCHX_CONTENT_BACKEND", "s3")
    monkeypatch.setenv("RESEARCHX_S3_BUCKET", "private-research")
    assert isinstance(content_store(), S3ContentStore)
    monkeypatch.setenv("RESEARCHX_CONTENT_BACKEND", "unknown")
    with pytest.raises(ValueError, match="local or s3"):
        content_store()
    with pytest.raises(ValueError, match="endpoint"):
        S3ContentStore(S3ContentConfig("https://key:secret@example.invalid", "private"))


async def test_local_batch_rechecks_paths_after_replacement_and_rejects_foreign_keys(tmp_path):
    from dataclasses import replace
    from researchx.storage.content import LocalContentStore
    store = LocalContentStore(tmp_path / "objects")
    first = await store.put("a" * 16, b"first")
    second = await store.put("a" * 16, b"second")
    assert await store.exists_many([first, second])
    with pytest.raises(ValueError, match="scope"):
        await store.exists_many([replace(first, workspace_id="b" * 16)])
    path = store.root / first.object_key
    body = path.read_bytes()
    path.unlink()
    assert not await store.exists_many([first, second])
    outside = tmp_path / "outside"
    outside.write_bytes(body)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        await store.exists_many([first, second])
    path.unlink()
    path.write_bytes(body)
    directory = path.parent
    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        await store.exists_many([first, second])
