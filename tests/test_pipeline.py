import json

import pytest

from rolki.config import Descriptions
from rolki.errors import PermanentError, ResourceWait
from rolki.pipeline import Pipeline
from rolki.subtitles import Cue, Word
from rolki.worker import execute


async def test_buffer_plan_before_render_and_retention(
    db, config, enqueue, fake_media, monkeypatch
):
    from rolki.buffer_store import BufferStore
    from rolki.config import Buffer

    config = config.model_copy(
        update={
            "buffer": Buffer(
                enabled=True,
                organization_id="org",
                channels=[{"id": "ig", "platform": "instagram"}],
            )
        }
    )
    monkeypatch.setenv("BUFFER_API_KEY", "test-key")
    calls, _storage = fake_media

    class Client:
        def __init__(self, _session):
            pass

        async def verify_channels(self, _settings):
            assert calls["metadata"] == 1
            assert calls["download"] == 0

        async def posts(self, _settings):
            return []

    monkeypatch.setattr("rolki.buffer_publisher.BufferClient", Client)
    job_id = enqueue(config=config)
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "done"
    store = BufferStore(db)
    assert len(store.plans()) == 2
    assert all(not p["variant"] for p in store.plans())
    assert "scissors" in db.notification_for_event(job_id, "chapter:0")["message"]
    result = json.loads(db.get(job_id)["checkpoint"])["results"]["0"]
    assert result["variants"]["crop"]["expires_at"] > store.plan(job_id, 0)["due_at"]
    assert not store.deliveries(job_id, 0)


@pytest.fixture
def fake_media(monkeypatch):
    from rolki import pipeline

    calls = {"metadata": 0, "download": 0, "transcribe": 0, "render": [], "upload": []}

    async def process(args, **_kwargs):
        operation = args[3]
        from pathlib import Path

        root = Path(args[5])
        calls[operation] += 1
        if operation == "metadata":
            result = {
                "title": "Film testowy",
                "duration": 20,
                "chapters": [
                    {"start_time": 0, "title": "Pierwszy"},
                    {"start_time": 10, "title": "Drugi"},
                ],
            }
        else:
            (root / "source.mkv").write_bytes(b"source")
            result = {"source": "source.mkv"}
        (root / f"{operation}.json").write_text(json.dumps(result))

    async def probe(_source):
        return {"streams": [{"codec_type": "audio"}], "format": {"duration": "20"}}

    async def transcribe(_source, _chapter, _root, _config):
        calls["transcribe"] += 1
        return [
            Cue(
                0,
                2,
                "Zażółć gęślą jaźń",
                (
                    Word(0.1, 0.5, "Zażółć"),
                    Word(0.6, 1.1, "gęślą"),
                    Word(1.2, 1.8, "jaźń"),
                ),
            )
        ]

    async def render(_source, chapter, root, _config, variant):
        calls["render"].append((chapter["index"], variant))
        output = root / f"{variant}.mp4"
        output.write_bytes(b"rendered")
        return output

    class Storage:
        fail_key = None

        def __init__(self, _config):
            pass

        async def upload(self, _path, key):
            calls["upload"].append(key)
            if self.fail_key and self.fail_key in key:
                raise PermanentError("S3 odrzuciło operację.")
            return "https://clips.example/" + key

    monkeypatch.setattr(pipeline, "run_process", process)
    monkeypatch.setattr(pipeline, "probe", probe)
    monkeypatch.setattr(pipeline, "transcribe", transcribe)
    monkeypatch.setattr(pipeline, "render", render)
    monkeypatch.setattr(pipeline, "S3Storage", Storage)
    return calls, Storage


async def test_pipeline_two_variants_per_chapter(db, config, enqueue, fake_media, monkeypatch):
    monkeypatch.setattr("rolki.pipeline.current_output_date", lambda: "2026-10-09")
    calls, _ = fake_media
    job_id = enqueue()
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "done"
    assert len(calls["render"]) == 4
    assert calls["transcribe"] == 2
    assert calls["download"] == 1
    assert {key.rsplit("/", 1)[1] for key in calls["upload"]} == {
        "2026-10-09-pierwszy-crop.mp4",
        "2026-10-09-pierwszy-letterboxed.mp4",
        "2026-10-09-drugi-crop.mp4",
        "2026-10-09-drugi-letterboxed.mp4",
    }
    state = json.loads(db.get(job_id)["checkpoint"])
    assert all(set(r["variants"]) == {"crop", "letterbox"} for r in state["results"].values())
    assert not (config.paths.work_dir / job_id).exists()
    messages = db.pending_notifications()
    assert len(messages) == 5
    assert all(message["root_id"] == "root" for message in messages)
    started = next(message for message in messages if message["event_key"] == "metadata")
    assert started["update_of"] == "accepted"
    assert "**1**" in started["message"] and job_id[:8] in started["message"]
    assert "Film: Film testowy." in started["message"]
    assert "Rozdziałów: 2. Rozpoczynam przetwarzanie." in started["message"]


async def test_descriptions_use_each_transcript_and_resume_without_paid_repeat(
    db,
    config,
    enqueue,
    fake_media,
    monkeypatch,
):
    calls, _ = fake_media
    config = config.model_copy(update={"descriptions": Descriptions(enabled=True)})
    requested = []
    fail = True

    async def generate(title, transcript, settings):
        requested.append((title, transcript))
        assert settings.model == "gpt-6.1-sol" and settings.reasoning_effort == "low"
        if title == "Drugi" and fail:
            raise PermanentError("Opis chwilowo niedostępny.")
        return f"Opis: {title}.\n\n#rozdział"

    monkeypatch.setattr("rolki.pipeline.generate_description", generate)
    job_id = enqueue(config=config)
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "failed"
    assert db.notification_for_event(job_id, "chapter:1") is not None
    assert db.notification_for_event(job_id, "description:1") is None
    assert len(calls["upload"]) == 4
    fail = False
    db.retry(job_id)
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "done"
    assert requested == [
        ("Pierwszy", "Zażółć gęślą jaźń"),
        ("Drugi", "Zażółć gęślą jaźń"),
        ("Drugi", "Zażółć gęślą jaźń"),
    ]
    assert len(calls["upload"]) == 4 and calls["transcribe"] == 2
    for index, title in enumerate(("Pierwszy", "Drugi")):
        post = db.notification_for_event(job_id, f"description:{index}")
        assert post["message"] == f"Opis: {title}.\n\n#rozdział"
        assert post["root_id"] == "root" and post["after_event"] == f"chapter:{index}"
    assert db.notification_for_event(job_id, "chapter:1")["after_event"] == "description:0"
    assert db.notification_for_event(job_id, "complete")["after_event"] == "description:1"
    checkpoint = json.loads(db.get(job_id)["checkpoint"])
    assert checkpoint["results"]["0"]["description"] == "Opis: Pierwszy.\n\n#rozdział"


async def test_silent_chapter_publishes_clips_without_inventing_description(
    db,
    config,
    enqueue,
    fake_media,
    monkeypatch,
):
    config = config.model_copy(update={"descriptions": Descriptions(enabled=True)})

    async def silence(*_):
        return []

    async def never_call(*_):
        pytest.fail("No speech must not generate a paid or invented description")

    monkeypatch.setattr("rolki.pipeline.transcribe", silence)
    monkeypatch.setattr("rolki.pipeline.generate_description", never_call)
    job_id = enqueue(config=config)
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "done"
    assert "Brak rozpoznanej mowy" in db.notification_for_event(job_id, "description:0")["message"]


async def test_resume_partial_upload_no_retranscription(db, enqueue, fake_media, monkeypatch):
    monkeypatch.setattr("rolki.pipeline.current_output_date", lambda: "2026-10-09")
    calls, storage = fake_media
    storage.fail_key = "pierwszy-letterboxed.mp4"
    job_id = enqueue()
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "failed"
    state = json.loads(db.get(job_id)["checkpoint"])
    assert state["output_date"] == "2026-10-09"
    assert state["results"]["0"]["upload_keys"]["letterbox"].endswith(
        "2026-10-09-pierwszy-letterboxed.mp4"
    )
    assert "crop" in json.loads(db.get(job_id)["checkpoint"])["results"]["0"]["variants"]
    saved = json.loads(db.get(job_id)["checkpoint"])["results"]["0"]["cues"][0]
    assert saved["words"][0] == {"start": 0.1, "end": 0.5, "text": "Zażółć"}
    storage.fail_key = None
    monkeypatch.setattr("rolki.pipeline.current_output_date", lambda: "2026-10-10")
    db.retry(job_id)
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "done"
    assert calls["transcribe"] == 2
    assert all("2026-10-09-" in key for key in calls["upload"])
    assert len(set(calls["upload"])) == 4
    assert calls["render"].count((0, "crop")) == 1
    assert (
        calls["render"].count((0, "letterbox")) == 1
    )  # Reuse completed render after failed upload.


async def test_duplicate_chapter_titles_have_distinct_paths(db, enqueue, fake_media, monkeypatch):
    from rolki import pipeline

    validate = pipeline.validate_chapters
    monkeypatch.setattr(
        pipeline,
        "validate_chapters",
        lambda info, limit: [
            {**chapter, "title": "Ten sam tytuł"} for chapter in validate(info, limit)
        ],
    )
    calls, _ = fake_media
    enqueue()
    await execute(db, Pipeline(db), db.claim())
    assert len(set(calls["upload"])) == 4
    assert len({key.rsplit("/", 1)[1] for key in calls["upload"]}) == 2


async def test_resource_wait_does_not_fail_or_spam(db, enqueue, monkeypatch):
    from rolki import pipeline

    def unavailable(*_args, **_kwargs):
        raise ResourceWait("Za mało RAM.")

    monkeypatch.setattr(pipeline, "check_resources", unavailable)
    monkeypatch.setattr(pipeline, "S3Storage", lambda _config: None)
    job_id = enqueue()
    for _ in range(2):
        if db.get(job_id)["status"] == "waiting":
            db.retry(job_id)
        await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "waiting"
    assert len(db.pending_notifications()) == 2


async def test_no_chapters_stops_before_download(db, enqueue, fake_media, monkeypatch):
    from rolki import pipeline

    def missing(_metadata, _max):
        raise PermanentError("Film nie ma rozdziałów.")

    monkeypatch.setattr(pipeline, "validate_chapters", missing)
    calls, _ = fake_media
    job_id = enqueue()
    await execute(db, Pipeline(db), db.claim())
    assert db.get(job_id)["status"] == "failed"
    assert calls["download"] == 0
