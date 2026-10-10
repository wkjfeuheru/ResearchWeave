"""Content-addressed objects, separate from structured PostgreSQL records."""

from __future__ import annotations

import asyncio
import hashlib
import re
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from dataclasses import asdict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from researchx.storage.database import Database
from researchx.storage.schema import content_objects
from researchx.config.paths import get_data_dir

from researchx.storage.filesystem import atomic_write_bytes, private_directory


@dataclass(frozen=True)
class ContentReference:
    workspace_id: str
    content_hash: str
    object_key: str
    size: int
    media_type: str = "application/octet-stream"


class ContentStore(Protocol):
    async def put(
        self, workspace: str, content: bytes, *, media_type: str = "application/octet-stream"
    ) -> ContentReference: ...

    async def read(self, reference: ContentReference) -> bytes: ...

    async def verify(self, reference: ContentReference) -> None: ...

    async def exists(self, reference: ContentReference) -> bool: ...

    async def exists_many(self, references: list[ContentReference]) -> bool: ...


@dataclass(frozen=True)
class S3ContentConfig:
    """Configuration contract for a deployment-supplied S3 ContentStore adapter.

    Credentials must use the provider credential chain, never object keys or DB rows.
    An adapter must finish PUT and checksum verification before returning from put().
    """

    endpoint: str
    bucket: str
    prefix: str = "researchx"
    region: str = "us-east-1"


class LocalContentStore:
    def __init__(self, root: Path) -> None:
        self.root = private_directory(root).resolve()

    def _path(self, workspace: str, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16,64}", workspace):
            raise ValueError("Invalid content workspace")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid content hash")
        path = self.root / workspace / digest[:2] / digest
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("Content paths must not contain symlinks")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Content path outside store")
        return path

    async def put(
        self, workspace: str, content: bytes, *, media_type: str = "application/octet-stream"
    ) -> ContentReference:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(workspace, digest)

        def write() -> None:
            private_directory(path.parent)
            if not path.exists():
                atomic_write_bytes(path, content, mode=0o600)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Content hash mismatch")
            if os.name == "posix":
                for directory in (path.parent, *path.parent.parents):
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    if directory == self.root.parent:
                        break

        await asyncio.to_thread(write)
        return ContentReference(
            workspace, digest, path.relative_to(self.root).as_posix(), len(content), media_type
        )

    async def read(self, reference: ContentReference) -> bytes:
        path = self._path(reference.workspace_id, reference.content_hash)
        if path.relative_to(self.root).as_posix() != reference.object_key:
            raise ValueError("Content reference scope mismatch")
        data = await asyncio.to_thread(path.read_bytes)
        if (
            len(data) != reference.size
            or hashlib.sha256(data).hexdigest() != reference.content_hash
        ):
            raise ValueError("Content hash mismatch")
        return data

    async def exists(self, reference: ContentReference) -> bool:
        path = self._path(reference.workspace_id, reference.content_hash)
        if path.relative_to(self.root).as_posix() != reference.object_key:
            raise ValueError("Content reference scope mismatch")
        return await asyncio.to_thread(path.is_file)

    async def exists_many(self, references: list[ContentReference]) -> bool:
        def inspect() -> bool:
            # One worker and one check per shared directory for this read only.
            # Never retain filesystem validation across calls: a later symlink
            # replacement or missing object must still invalidate the next load.
            if self.root.is_symlink() or self.root.resolve() != self.root:
                raise ValueError("Content path outside store")
            checked: set[Path] = set()
            for reference in references:
                workspace, digest = reference.workspace_id, reference.content_hash
                if not re.fullmatch(r"[a-f0-9]{16,64}", workspace):
                    raise ValueError("Invalid content workspace")
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("Invalid content hash")
                if reference.object_key != f"{workspace}/{digest[:2]}/{digest}":
                    raise ValueError("Content reference scope mismatch")
                directory = self.root / workspace / digest[:2]
                for parent in (directory.parent, directory):
                    if parent not in checked:
                        if parent.is_symlink():
                            raise ValueError("Content paths must not contain symlinks")
                        checked.add(parent)
                path = directory / digest
                if path.is_symlink():
                    raise ValueError("Content paths must not contain symlinks")
                if not path.is_file():
                    return False
            return True

        return await asyncio.to_thread(inspect)

    async def verify(self, reference: ContentReference) -> None:
        await self.read(reference)


async def persist_object(database: Database, workspace: str, content: bytes) -> str:
    store = content_store()
    reference = await store.put(workspace, content)
    await store.verify(reference)
    async with database.transaction() as db:
        await db.execute(
            insert(content_objects).values(**asdict(reference)).on_conflict_do_nothing()
        )
        existing = (
            (
                await db.execute(
                    select(content_objects).where(
                        content_objects.c.workspace_id == workspace,
                        content_objects.c.content_hash == reference.content_hash,
                    )
                )
            )
            .mappings()
            .one()
        )
        if existing["object_key"] != reference.object_key or existing["size"] != reference.size:
            raise ValueError("Existing content reference conflicts with object")
    return "sha256:" + reference.content_hash


async def read_object(database: Database, workspace: str, key: str) -> bytes:
    if not key.startswith("sha256:"):
        raise ValueError("Invalid content reference; import legacy receipts explicitly")
    digest = key.removeprefix("sha256:")
    async with database.transaction() as db:
        row = (
            (
                await db.execute(
                    select(content_objects).where(
                        content_objects.c.workspace_id == workspace,
                        content_objects.c.content_hash == digest,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise FileNotFoundError("Content reference is missing")
    return await content_store().read(ContentReference(**dict(row)))


class S3ContentStore:
    """S3-compatible immutable objects; SDK I/O runs outside the event loop.

    Bucket/endpoint are deployment configuration, never agent-controlled values.
    The optional SDK uses its credential chain and bounded, non-retrying requests.
    """

    def __init__(self, config: S3ContentConfig) -> None:
        from urllib.parse import urlsplit

        endpoint = urlsplit(config.endpoint)
        if (
            endpoint.scheme not in {"https", "http"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("Invalid S3 endpoint configuration")
        if not config.bucket or not re.fullmatch(r"[A-Za-z0-9._-]+", config.bucket):
            raise ValueError("Invalid S3 bucket configuration")
        if not re.fullmatch(r"[A-Za-z0-9/_-]*", config.prefix):
            raise ValueError("Invalid S3 prefix configuration")
        self.config = config

    def _key(self, workspace: str, digest: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{16,64}", workspace) or not re.fullmatch(
            r"[a-f0-9]{64}", digest
        ):
            raise ValueError("Invalid content scope or hash")
        return f"{workspace}/{digest[:2]}/{digest}"

    def _client(self) -> object:
        import importlib

        try:
            boto = importlib.import_module("boto3")
            config = importlib.import_module("botocore.config").Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
            )
        except ImportError:
            raise RuntimeError("S3 内容后端需要安装 researchx-ai[s3]") from None
        return boto.client(
            "s3", endpoint_url=self.config.endpoint, region_name=self.config.region, config=config
        )

    def _remote_key(self, key: str) -> str:
        return "/".join(part for part in (self.config.prefix.strip("/"), key) if part)

    async def put(
        self, workspace: str, content: bytes, *, media_type: str = "application/octet-stream"
    ) -> ContentReference:
        import importlib
        from typing import Any, cast

        digest = hashlib.sha256(content).hexdigest()
        reference = ContentReference(
            workspace, digest, self._key(workspace, digest), len(content), media_type
        )

        def write() -> None:
            client = cast(Any, self._client())
            error = importlib.import_module("botocore.exceptions").ClientError
            try:
                try:
                    client.put_object(
                        Bucket=self.config.bucket,
                        Key=self._remote_key(reference.object_key),
                        Body=content,
                        ContentType=media_type,
                        IfNoneMatch="*",
                        Metadata={"sha256": digest},
                    )
                except error as exc:
                    if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                        raise OSError("S3 content write failed") from None
            finally:
                client.close()

        await asyncio.to_thread(write)
        await self.verify(reference)
        return reference

    async def read(self, reference: ContentReference) -> bytes:
        from typing import Any, cast
        import importlib

        if self._key(reference.workspace_id, reference.content_hash) != reference.object_key:
            raise ValueError("Content reference scope mismatch")

        def fetch() -> bytes:
            client = cast(Any, self._client())
            error = importlib.import_module("botocore.exceptions").ClientError
            try:
                try:
                    response = client.get_object(
                        Bucket=self.config.bucket, Key=self._remote_key(reference.object_key)
                    )
                except error:
                    raise OSError("S3 content read failed") from None
                stream = response["Body"]
                try:
                    data = cast(bytes, stream.read(reference.size + 1))
                finally:
                    stream.close()
            finally:
                client.close()
            if (
                len(data) != reference.size
                or hashlib.sha256(data).hexdigest() != reference.content_hash
            ):
                raise ValueError("Content hash mismatch")
            return data

        return await asyncio.to_thread(fetch)

    async def exists(self, reference: ContentReference) -> bool:
        import importlib
        from typing import Any, cast

        if self._key(reference.workspace_id, reference.content_hash) != reference.object_key:
            raise ValueError("Content reference scope mismatch")

        def head() -> bool:
            client = cast(Any, self._client())
            error = importlib.import_module("botocore.exceptions").ClientError
            try:
                try:
                    client.head_object(
                        Bucket=self.config.bucket, Key=self._remote_key(reference.object_key)
                    )
                    return True
                except error as exc:
                    if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                        return False
                    raise OSError("S3 content metadata unavailable") from None
            finally:
                client.close()

        return await asyncio.to_thread(head)

    async def exists_many(self, references: list[ContentReference]) -> bool:
        for reference in references:
            if not await self.exists(reference):
                return False
        return True

    async def verify(self, reference: ContentReference) -> None:
        await self.read(reference)


def content_store() -> ContentStore:
    backend = os.environ.get("RESEARCHX_CONTENT_BACKEND", "local")
    if backend == "local":
        root = Path(os.environ.get("RESEARCHX_CONTENT_ROOT", str(get_data_dir() / "objects")))
        return LocalContentStore(root)
    if backend == "s3":
        return S3ContentStore(
            S3ContentConfig(
                endpoint=os.environ.get("RESEARCHX_S3_ENDPOINT", "https://s3.amazonaws.com"),
                bucket=os.environ.get("RESEARCHX_S3_BUCKET", ""),
                prefix=os.environ.get("RESEARCHX_S3_PREFIX", "researchx"),
                region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            )
        )
    raise ValueError("RESEARCHX_CONTENT_BACKEND must be local or s3")


async def materialize_object(
    database: Database, workspace: str, key: str, directory: Path, filename: str
) -> Path:
    """Verified, disposable file projection; callers never reload authority from this cache."""
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("Invalid content projection filename")
    directory = private_directory(directory)
    path = directory / filename
    if path.is_symlink():
        raise ValueError("Content projection must not be a symlink")
    body = await read_object(database, workspace, key)
    await asyncio.to_thread(atomic_write_bytes, path, body, mode=0o600)
    return path
