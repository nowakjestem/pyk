from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Mattermost(Settings):
    url: str = ""
    channel_ids: list[str] = Field(default_factory=list)
    reconnect_seconds: int = Field(default=5, ge=1)
    reconcile_seconds: int = Field(default=60, ge=5)
    health_port: int = Field(default=8089, ge=1, le=65535)

    @field_validator("channel_ids", mode="before")
    @classmethod
    def channels(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value or []

    @field_validator("url")
    @classmethod
    def http_url(cls, value):
        return validate_url(value)


class Paths(Settings):
    database: Path = Path("data/state.sqlite3")
    work_dir: Path = Path("data/work")
    output_dir: Path = Path("data/output")


class ASR(Settings):
    # Keep local as the schema default for jobs saved before API support.
    provider: Literal["local", "openai"] = "local"
    model: Literal["whisper-1", "gpt-transcribe"] = "whisper-1"
    text_chunk_seconds: int = Field(default=8, ge=3, le=15)
    concurrency: int = Field(default=1, ge=1, le=8)
    prompt: str = Field(default="", max_length=1024)
    request_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    binary: str = "whisper-cli"
    quantizer: str = "whisper-quantize"
    model_path: Path = Path("models/ggml-base-q5_0.bin")
    model_url: str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
    quantization: Literal["q5_0", "q5_1", "q8_0"] = "q5_0"
    language: str = "pl"
    threads: int = Field(default=2, ge=1, le=4)
    max_chunk_seconds: int = Field(default=300, ge=30, le=600)
    timeout_seconds: int = Field(default=7200, ge=30)

    @field_validator("language")
    @classmethod
    def language_code(cls, value):
        if not re.fullmatch(r"[a-z]{2,3}|auto", value):
            raise ValueError("language must be a language code or auto")
        return value

    @field_validator("model_url")
    @classmethod
    def download_url(cls, value):
        return validate_url(value)


class Descriptions(Settings):
    # Existing job snapshots must not start making additional paid requests.
    enabled: bool = False
    model: str = Field(default="gpt-6.1-sol", min_length=1, max_length=100)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    request_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    max_output_tokens: int = Field(default=4096, ge=512, le=16384)
    max_chars: int = Field(default=1800, ge=100, le=2000)
    prompt: str = Field(
        default=(
            "Napisz po polsku jeden opis do publikacji tego rozdziału jako rolki "
            "na Instagramie lub TikToku. Zacznij od krótkiego, konkretnego zdania "
            "przyciągającego uwagę, potem dodaj 2–4 krótkie zdania i 3–5 trafnych hashtagów. "
            "Pisz naturalnie, bez clickbaitu, bez wymyślonych cytatów i faktów. "
            "Opinie przedstawiaj jako opinie autora nagrania. "
            "Zwróć wyłącznie gotowy opis: bez etykiet, komentarzy, cudzysłowów, "
            "formatowania Markdown ani bloków kodu."
        ),
        min_length=1,
        max_length=8000,
    )


class Video(Settings):
    width: int = Field(default=720, ge=144, le=2160)
    height: int = Field(default=1280, ge=256, le=3840)
    source_max_height: int = Field(default=1080, ge=144, le=2160)
    crop_x: float = Field(default=0.5, ge=0, le=1)
    crop_y: float = Field(default=0.5, ge=0, le=1)
    crf: int = Field(default=23, ge=0, le=51)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"] = (
        "veryfast"
    )
    fps: int = Field(default=30, ge=1, le=60)
    threads: int = Field(default=2, ge=1, le=4)
    timeout_seconds: int = Field(default=7200, ge=30)

    @model_validator(mode="after")
    def vertical(self):
        if self.width % 2 or self.height % 2 or self.width * 16 != self.height * 9:
            raise ValueError("output must have even dimensions and 9:16 aspect ratio")
        return self


class Position(Settings):
    alignment: int = Field(default=2, ge=1, le=9)
    margin_v: int = Field(default=130, ge=0)


class SubtitleBackground(Settings):
    mode: Literal["none", "line", "word"] = "none"
    color: str = "#000000"
    opacity: float = Field(default=0.8, ge=0, le=1)
    padding: float = Field(default=6, ge=0, le=30)

    @field_validator("color")
    @classmethod
    def background_color(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("use #RRGGBB colors")
        return value


class Subtitles(Settings):
    font: str = "Lato"
    font_size: int = Field(default=42, ge=8, le=180)
    primary_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: float = Field(default=3, ge=0, le=10)
    shadow: float = Field(default=0, ge=0, le=10)
    bold: bool = True
    max_lines: int = Field(default=2, ge=1, le=2)
    max_chars_per_line: int = Field(default=26, ge=8, le=80)
    max_phrase_seconds: float = Field(default=4, ge=0.5, le=10)
    margin_x: int = Field(default=40, ge=0)
    background: SubtitleBackground = Field(default_factory=SubtitleBackground)
    crop: Position = Field(default_factory=Position)
    letterbox: Position = Field(default_factory=lambda: Position(margin_v=230))

    @field_validator("primary_color", "outline_color")
    @classmethod
    def color(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("use #RRGGBB colors")
        return value

    @field_validator("font")
    @classmethod
    def font_name(cls, value):
        if not value or any(c in value for c in ",\n\r"):
            raise ValueError("invalid font name")
        return value


class S3(Settings):
    endpoint_url: str = ""
    bucket: str = ""
    region: str = "eu-central-1"
    public_base_url: str = ""
    prefix: str = "clips"
    retention_days: int = Field(default=30, ge=1, le=3650)
    addressing_style: Literal["auto", "path", "virtual"] = "auto"

    @field_validator("endpoint_url", "public_base_url")
    @classmethod
    def http_url(cls, value):
        return validate_url(value)

    @field_validator("prefix")
    @classmethod
    def safe_prefix(cls, value):
        value = value.strip("/")
        if not value or not re.fullmatch(r"[a-zA-Z0-9/_-]+", value):
            raise ValueError("prefix must be a nonempty safe S3 path")
        return value


class Limits(Settings):
    max_video_seconds: int = Field(default=3600, ge=1)
    max_work_bytes: int = Field(default=8 * 1024**3, ge=1024**2)
    min_disk_free_bytes: int = Field(default=5 * 1024**3, ge=0)
    min_available_memory_bytes: int = Field(default=768 * 1024**2, ge=0)
    min_container_headroom_bytes: int = Field(default=512 * 1024**2, ge=0)
    failed_retention_hours: int = Field(default=24, ge=1)
    resource_retry_seconds: int = Field(default=60, ge=1)
    download_timeout_seconds: int = Field(default=1800, ge=30)


class Worker(Settings):
    poll_seconds: int = Field(default=5, ge=1)
    shutdown_grace_seconds: int = Field(default=30, ge=1)


class BufferSchedule(Settings):
    timezone: str = "Europe/Warsaw"
    window_start: time = time(10)
    window_end: time = time(20)
    min_gap_minutes: int = Field(default=180, ge=1, le=1440)
    max_posts_per_day: int = Field(default=2, ge=1, le=24)
    min_lead_minutes: int = Field(default=120, ge=5)

    @model_validator(mode="after")
    def valid_window(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid Buffer timezone") from exc
        if self.window_start >= self.window_end:
            raise ValueError("Buffer window must start before it ends on the same day")
        if any(t.tzinfo or t.second or t.microsecond for t in (self.window_start, self.window_end)):
            raise ValueError("Buffer window must use local hours and minutes")
        return self


class BufferChannel(Settings):
    id: str = Field(min_length=1)
    platform: Literal["instagram", "tiktok", "youtube"]
    max_video_seconds: int = Field(default=180, ge=1)
    max_text_chars: int = Field(default=2000, ge=1)
    should_share_to_feed: bool = True
    category_id: str = "22"
    privacy: Literal["public", "private", "unlisted"] = "public"
    made_for_kids: bool = False


class Buffer(Settings):
    enabled: bool = False
    organization_id: str = ""
    scheduling_mode: Literal["addToQueue", "customScheduled"] = "addToQueue"
    reactions: dict[str, Literal["crop", "letterbox"]] = Field(
        default_factory=lambda: {"scissors": "crop", "frame_with_picture": "letterbox"}
    )
    channels: list[BufferChannel] = Field(default_factory=list)
    schedule: BufferSchedule = Field(default_factory=BufferSchedule)
    retention_margin_hours: int = Field(default=72, ge=1)
    poll_seconds: int = Field(default=30, ge=5)
    status_poll_seconds: int = Field(default=3600, ge=300)

    @model_validator(mode="after")
    def valid_buffer(self):
        if len({c.id for c in self.channels}) != len(self.channels):
            raise ValueError("duplicate Buffer channel")
        if not self.reactions or any(not re.fullmatch(r"[a-z0-9_+-]+", e) for e in self.reactions):
            raise ValueError("invalid Buffer reaction names")
        if self.enabled and not (self.organization_id and self.channels):
            raise ValueError("Buffer requires organization_id and channels")
        return self


class Config(Settings):
    mattermost: Mattermost = Field(default_factory=Mattermost)
    paths: Paths = Field(default_factory=Paths)
    asr: ASR = Field(default_factory=ASR)
    descriptions: Descriptions = Field(default_factory=Descriptions)
    video: Video = Field(default_factory=Video)
    subtitles: Subtitles = Field(default_factory=Subtitles)
    s3: S3 = Field(default_factory=S3)
    limits: Limits = Field(default_factory=Limits)
    worker: Worker = Field(default_factory=Worker)
    buffer: Buffer = Field(default_factory=Buffer)

    @model_validator(mode="after")
    def margins(self):
        if self.subtitles.margin_x * 2 >= self.video.width:
            raise ValueError("subtitle horizontal margins exceed output width")
        if (
            max(self.subtitles.crop.margin_v, self.subtitles.letterbox.margin_v)
            >= self.video.height
        ):
            raise ValueError("subtitle vertical margins exceed output height")
        return self

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]

    def require_integrations(self):
        if not self.mattermost.url or not self.mattermost.channel_ids:
            raise ValueError("Uzupełnij MATTERMOST_URL i MATTERMOST_CHANNEL_IDS.")
        if not os.getenv("MATTERMOST_BOT_TOKEN"):
            raise ValueError("Brak MATTERMOST_BOT_TOKEN w .env.")
        self.require_storage()

    def require_storage(self):
        if not self.s3.bucket or not self.s3.public_base_url:
            raise ValueError("Uzupełnij S3_BUCKET i S3_PUBLIC_BASE_URL.")

    def require_asr(self):
        if (self.asr.provider == "openai" or self.descriptions.enabled) and not os.getenv(
            "OPENAI_API_KEY", ""
        ).strip():
            raise ValueError("Brak OPENAI_API_KEY w .env.")

    def require_buffer(self):
        if self.buffer.enabled and not os.getenv("BUFFER_API_KEY", "").strip():
            raise ValueError("Brak BUFFER_API_KEY w otoczeniu procesu.")


def validate_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("expected an HTTP(S) URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URLs cannot contain query strings or fragments")
    return value.rstrip("/")


def load_config(path: Path | str = "config.yaml") -> Config:
    path = Path(path).resolve()
    load_dotenv(path.parent / ".env", override=False)
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def expand(value):
        if isinstance(value, str):
            return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.getenv(m[1], ""), value)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    config = Config.model_validate(expand(document))
    paths = {
        key: str((path.parent / value).resolve())
        for key, value in config.paths.model_dump().items()
    }
    asr = config.asr.model_dump(mode="json")
    asr["model_path"] = str((path.parent / config.asr.model_path).resolve())
    return Config.model_validate(
        {**json.loads(config.model_dump_json()), "paths": paths, "asr": asr}
    )
