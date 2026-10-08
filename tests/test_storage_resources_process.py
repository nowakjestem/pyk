import asyncio
import os
import sys

import pytest
from botocore.exceptions import ClientError

from rolki.errors import ResourceWait
from rolki.process import run_process
from rolki.resources import check_resources
from rolki.storage import S3Storage, policies


class S3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return self.objects[Key]

    def upload_file(self, Filename, Bucket, Key, ExtraArgs, Config):
        from pathlib import Path

        self.uploads += 1
        self.objects[Key] = {
            "ContentLength": Path(Filename).stat().st_size,
            "Metadata": ExtraArgs["Metadata"],
        }


async def test_s3_idempotent_upload_and_checksum(config, tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"some video")
    client = S3Client()
    storage = S3Storage(config.s3, client)
    url = await storage.upload(path, "clips/job/000/crop.mp4")
    assert url == "https://clips.example/clips/job/000/crop.mp4"
    await storage.upload(path, "clips/job/000/crop.mp4")
    assert client.uploads == 1
    path.write_bytes(b"changed video")
    await storage.upload(path, "clips/job/000/crop.mp4")
    assert client.uploads == 2


async def test_aws_missing_object_without_list_bucket(config, tmp_path):
    class NoListBucketClient(S3Client):
        def head_object(self, Bucket, Key):
            if Key not in self.objects:
                raise ClientError(
                    {"Error": {"Code": "403"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                    "HeadObject",
                )
            return super().head_object(Bucket, Key)

    path = tmp_path / "video.mp4"
    path.write_bytes(b"some video")
    client = NoListBucketClient()
    await S3Storage(config.s3, client).upload(path, "clips/job/000/crop.mp4")
    assert client.uploads == 1


def test_policies_restrict_prefix_and_retention(config):
    document = policies(config.s3)
    assert document["lifecycle"]["Rules"][0]["Expiration"]["Days"] == 30
    assert document["bucket-policy"]["Statement"][0]["Resource"].endswith("/clips/*")
    assert "DeleteObject" not in str(document["application-policy"])


def test_resource_admission_checks_memory_disk_and_workdir(config, monkeypatch):
    from rolki import resources

    limited = config.model_copy(
        update={"limits": config.limits.model_copy(update={"min_available_memory_bytes": 100})}
    )
    monkeypatch.setattr(resources, "available_memory", lambda: 99)
    with pytest.raises(ResourceWait, match="RAM"):
        check_resources(limited)
    check_resources(limited, memory=False)
    config.paths.work_dir.mkdir(exist_ok=True)
    (config.paths.work_dir / "big").write_bytes(b"x" * 1024**2)
    limited = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_work_bytes": 1024**2})}
    )
    with pytest.raises(ResourceWait, match="limit"):
        check_resources(limited)


async def test_cancellation_stops_subprocess(tmp_path):
    pid_file = tmp_path / "pid"
    command = [
        sys.executable,
        "-c",
        "import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
        str(pid_file),
    ]
    task = asyncio.create_task(run_process(command, timeout=120))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
