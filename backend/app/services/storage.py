"""Object storage adapter.

The only provider-specific code in the application. Region, endpoint and bucket
are configuration, so a UAE-hosted or fully private target is a deployment
change rather than a code change. Keys are namespaced per tenant.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import BinaryIO

from app.core.config import get_settings


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    etag: str | None = None


class ObjectStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: BinaryIO | bytes, *, content_type: str) -> StoredObject: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def health(self) -> None: ...

    @staticmethod
    def tenant_key(org_id: uuid.UUID, *parts: str) -> str:
        """Every object lives under its tenant's prefix. No shared namespace."""
        return "/".join(("org", str(org_id), *parts))


class S3Storage(ObjectStorage):
    def __init__(self) -> None:
        import boto3

        s = get_settings()
        self._bucket = s.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=s.storage_endpoint_url,
            aws_access_key_id=s.storage_access_key,
            aws_secret_access_key=s.storage_secret_key,
            region_name=s.storage_region,
        )

    def put(self, key: str, data: BinaryIO | bytes, *, content_type: str) -> StoredObject:
        body = data if isinstance(data, bytes) else data.read()
        resp = self._client.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )
        return StoredObject(key=key, size=len(body), etag=resp.get("ETag"))

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def health(self) -> None:
        self._client.head_bucket(Bucket=self._bucket)

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)


class LocalDiskStorage(ObjectStorage):
    """Files under a directory, keyed exactly as the S3 backend keys them.

    Same key namespace as S3, so a store can be moved between the two without
    rewriting references. Keys are validated before use: a key is data that
    reaches this layer from upload paths, and `..` in one must not be able to
    walk out of the storage root.
    """

    def __init__(self, root: str | None = None) -> None:
        from pathlib import Path

        s = get_settings()
        self._root = Path(root or s.storage_local_path).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str):
        target = (self._root / key).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError(f"Refusing a storage key that escapes the root: {key!r}")
        return target

    def put(self, key: str, data: BinaryIO | bytes, *, content_type: str) -> StoredObject:
        body = data if isinstance(data, bytes) else data.read()
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return StoredObject(key=key, size=len(body))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        return self._path(key).as_uri()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def health(self) -> None:
        if not self._root.is_dir():
            raise RuntimeError(f"Storage root {self._root} does not exist.")

    def ensure_bucket(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)


class InMemoryStorage(ObjectStorage):
    """Test double. Never used outside tests."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: BinaryIO | bytes, *, content_type: str) -> StoredObject:
        body = data if isinstance(data, bytes) else data.read()
        self._objects[key] = body
        return StoredObject(key=key, size=len(body))

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        return f"memory://{key}?expires={expires_seconds}"

    def exists(self, key: str) -> bool:
        return key in self._objects

    def health(self) -> None:
        return None

    def keys(self) -> list[str]:
        """Everything stored, for tests that need to tamper with an output."""
        return list(self._objects)


@lru_cache
def get_storage() -> ObjectStorage:
    if get_settings().storage_backend == "local":
        return LocalDiskStorage()
    return S3Storage()
