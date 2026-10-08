from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from urllib.parse import quote

from .config import S3
from .errors import PermanentError, TransientError


class LocalStorage:
    """CLI-only output sink: run the real pipeline without AWS or Mattermost."""

    def __init__(self, root: Path):
        self.root = root

    async def upload(self, path: Path, key: str) -> str:
        import shutil

        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, path, destination)
        return str(destination.resolve())


class S3Storage:
    def __init__(self, config: S3, client=None):
        import boto3
        from botocore.config import Config

        self.config = config
        self.client = client or boto3.client(
            "s3",
            endpoint_url=config.endpoint_url or None,
            region_name=config.region,
            config=Config(
                connect_timeout=15,
                read_timeout=60,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": config.addressing_style},
            ),
        )

    def _upload(self, path: Path, key: str):
        from boto3.exceptions import S3UploadFailedError
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import BotoCoreError, ClientError

        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024**2):
                digest.update(chunk)
        checksum = digest.hexdigest()
        try:
            # Deterministic keys + digest allow recovery after an ambiguous successful upload.
            try:
                existing = self.client.head_object(Bucket=self.config.bucket, Key=key)
            except ClientError as exc:
                # Without ListBucket, AWS returns 403 for a missing key. PutObject +
                # final HEAD still verify both write/read permissions on our own key.
                if exc.response.get("Error", {}).get("Code") not in (
                    "403",
                    "AccessDenied",
                    "404",
                    "NoSuchKey",
                    "NotFound",
                ):
                    raise
                existing = None
            if not (
                existing
                and existing.get("ContentLength") == path.stat().st_size
                and existing.get("Metadata", {}).get("sha256") == checksum
            ):
                self.client.upload_file(
                    str(path),
                    self.config.bucket,
                    key,
                    ExtraArgs={
                        "ContentType": "video/mp4",
                        "Metadata": {"sha256": checksum},
                        "CacheControl": "public, max-age=86400",
                    },
                    Config=TransferConfig(
                        multipart_threshold=8 * 1024**2,
                        multipart_chunksize=8 * 1024**2,
                        max_concurrency=1,
                        use_threads=False,
                    ),
                )
            result = self.client.head_object(Bucket=self.config.bucket, Key=key)
            if (
                result.get("ContentLength") != path.stat().st_size
                or result.get("Metadata", {}).get("sha256") != checksum
            ):
                raise TransientError("Weryfikacja pliku w S3 nie powiodła się.")
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status == 429 or status >= 500:
                raise TransientError("Tymczasowy błąd S3.") from exc
            raise PermanentError(
                "S3 odrzuciło operację. Sprawdź bucket i uprawnienia konta."
            ) from exc
        except BotoCoreError as exc:
            raise TransientError("Błąd połączenia z S3.") from exc
        except S3UploadFailedError as exc:
            cause = exc.__context__
            if isinstance(cause, ClientError):
                status = cause.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                if status in (400, 401, 403, 404):
                    raise PermanentError(
                        "S3 odrzuciło upload. Sprawdź bucket i uprawnienia konta."
                    ) from exc
            raise TransientError("Upload do S3 nie powiódł się.") from exc
        return f"{self.config.public_base_url.rstrip('/')}/{quote(key, safe='/')}"

    async def upload(self, path: Path, key: str) -> str:
        return await asyncio.to_thread(self._upload, path, key)


def policies(config: S3) -> dict:
    resource = f"arn:aws:s3:::{config.bucket}/{config.prefix}/*"
    return {
        "bucket-policy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ReadClips",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": resource,
                }
            ],
        },
        "application-policy": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
                    "Resource": resource,
                },
            ],
        },
        "lifecycle": {
            "Rules": [
                {
                    "ID": "rolki-retention",
                    "Status": "Enabled",
                    "Filter": {"Prefix": config.prefix + "/"},
                    "Expiration": {"Days": config.retention_days},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    }
