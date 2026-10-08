from datetime import UTC, datetime

import pytest

from rolki import filenames
from rolki.filenames import clip_filename


@pytest.mark.parametrize(
    "title,variant,expected",
    [
        ("Łódź / Zażółć gęślą jaźń!", "crop", "lodz-zazolc-gesla-jazn-crop"),
        ("  Pierwszy_ROZDZIAŁ  ", "letterbox", "pierwszy-rozdzial-letterboxed"),
        ("../../ Test / .. ", "crop", "test-crop"),
        ("🎬!!!", "letterbox", "rozdzial-letterboxed"),
    ],
)
def test_filename_has_date_safe_slug_and_variant(title, variant, expected):
    assert clip_filename(title, variant, "2026-10-09") == f"2026-10-09-{expected}.mp4"


def test_long_filename_fits_filesystem_component():
    name = clip_filename("bardzo długi tytuł " * 100, "letterbox", "2026-10-09")
    assert len(name.encode()) < 255
    assert name.endswith("-letterboxed.mp4")
    assert "/" not in name and "--" not in name


def test_output_date_uses_warsaw_even_when_host_is_on_previous_day(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 22, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(filenames, "datetime", Clock)
    assert filenames.current_output_date() == "2026-10-09"
