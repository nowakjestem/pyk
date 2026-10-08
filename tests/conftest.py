import pytest

from rolki.config import Config
from rolki.db import Database


@pytest.fixture
def config(tmp_path):
    return Config.model_validate(
        {
            "paths": {
                "database": tmp_path / "db.sqlite",
                "work_dir": tmp_path / "work",
                "output_dir": tmp_path / "output",
            },
            "mattermost": {"url": "https://mattermost.example", "channel_ids": ["private"]},
            "s3": {"bucket": "test-clips", "public_base_url": "https://clips.example"},
            "limits": {
                "min_disk_free_bytes": 0,
                "min_available_memory_bytes": 0,
                "min_container_headroom_bytes": 0,
            },
        }
    )


@pytest.fixture
def db(config):
    return Database(config.paths.database)


@pytest.fixture
def enqueue(db, config):
    def add(**kwargs):
        return db.enqueue(
            **(
                {
                    "post_id": "post",
                    "video_id": "abcdefghijk",
                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "config": config,
                    "channel_id": "private",
                    "root_id": "root",
                }
                | kwargs
            )
        )

    return add
