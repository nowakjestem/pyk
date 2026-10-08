import json

import pytest
from pydantic import ValidationError

from rolki.config import Config, load_config
from rolki.urls import youtube_links


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk&t=30&list=some-list",
        "https://youtu.be/abcdefghijk?si=test",
        "https://youtube.com/shorts/abcdefghijk",
        "https://m.youtube.com/watch?v=abcdefghijk",
    ],
)
def test_supported_urls(url):
    assert youtube_links(f"Film: [{url}]({url}).") == [
        ("abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk")
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com.evil.example/watch?v=abcdefghijk",
        "https://localhost/watch?v=abcdefghijk",
        "https://youtube.com/playlist?list=abc",
        "https://youtube.com:bad/watch?v=abcdefghijk",
        "https://user:password@youtu.be/abcdefghijk",
        "https://youtube.com/watch?v=../../file",
        "https://youtu.be/not-video",
    ],
)
def test_reject_arbitrary_urls(url):
    assert youtube_links(url) == []


def test_multiple_urls_dedup():
    assert (
        len(
            youtube_links(
                "https://youtu.be/abcdefghijk https://youtube.com/shorts/abcdefghijk https://youtu.be/12345678901"
            )
        )
        == 2
    )


def test_yaml_environment_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERMOST_CHANNEL_IDS", "first, second")
    monkeypatch.setenv("MATTERMOST_BOT_TOKEN", "secret-not-in-config")
    path = tmp_path / "config.yaml"
    path.write_text(
        "mattermost:\n  channel_ids: ${MATTERMOST_CHANNEL_IDS}\nsubtitles:\n  font_size: 51\n"
    )
    config = load_config(path)
    assert config.mattermost.channel_ids == ["first", "second"]
    assert config.subtitles.font_size == 51
    assert config.paths.database == tmp_path / "data/state.sqlite3"
    assert "secret-not-in-config" not in config.model_dump_json()
    assert Config.model_validate_json(config.model_dump_json()).revision == config.revision


@pytest.mark.parametrize(
    "change",
    [
        {"video": {"width": 721}},
        {"video": {"height": 720}},
        {"subtitles": {"max_lines": 3}},
        {"subtitles": {"primary_color": "bad"}},
        {"subtitles": {"font": "font,inject"}},
        {"s3": {"prefix": "../../root"}},
        {"mattermost": {"url": "https://user:secret@example.com"}},
        {"unknown": True},
    ],
)
def test_invalid_config(change):
    with pytest.raises(ValidationError):
        Config.model_validate(change)


def test_missing_integration_credentials(config, monkeypatch):
    monkeypatch.delenv("MATTERMOST_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TOKEN"):
        config.require_integrations()
    assert "token" not in json.loads(config.model_dump_json())["mattermost"]
