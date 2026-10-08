"""Isolated yt-dlp process: keep its imports and download memory out of the worker."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .errors import PermanentError, ResourceWait, TransientError


class QuietLogger:
    def debug(self, _message):
        pass

    info = debug
    warning = debug
    error = debug


def classify_error(message: str):
    lower = message.lower()
    if any(
        value in lower
        for value in (
            "sign in",
            "not available",
            "private video",
            "removed",
            "members-only",
            "age-restricted",
        )
    ):
        return PermanentError("Film jest niedostępny albo wymaga logowania.")
    if any(
        value in lower
        for value in (
            "timed out",
            "timeout",
            "connection",
            "429",
            "http error 5",
            "network",
            "temporary failure",
        )
    ):
        return TransientError("Tymczasowy błąd połączenia z YouTube.")
    return PermanentError(
        "Nie udało się pobrać filmu lub metadanych z YouTube. Sprawdź dostępność filmu i wersję yt-dlp."
    )


def main():
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["metadata", "download"])
    parser.add_argument("url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--min-free", type=int, required=True)
    args = parser.parse_args()
    from .urls import youtube_links

    if len(youtube_links(args.url)) != 1 or youtube_links(args.url)[0][1] != args.url:
        print("Nieobsługiwany URL.")
        return 65
    args.destination.mkdir(parents=True, exist_ok=True)

    def guard(_progress):
        from .resources import tree_size

        if shutil.disk_usage(args.work_root).free < args.min_free:
            raise ResourceWait("Za mało wolnego dysku podczas pobierania.")
        if tree_size(args.work_root) >= args.max_bytes:
            raise ResourceWait("Pobieranie osiągnęło limit katalogu roboczego.")

    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "socket_timeout": 30,
        "retries": 0,
        "fragment_retries": 0,
        "concurrent_fragment_downloads": 1,
        "js_runtimes": {"deno": {}},
        "format": f"bv*[height<={args.height}]+ba/b[height<={args.height}]",
        "outtmpl": str(args.destination / "source.%(ext)s"),
        "merge_output_format": "mkv",
        "progress_hooks": [guard],
        "postprocessor_hooks": [guard],
        "max_filesize": args.max_bytes,
        "overwrites": False,
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(args.url, download=args.operation == "download")
        if args.operation == "metadata":
            result = {
                key: info.get(key)
                for key in (
                    "id",
                    "title",
                    "duration",
                    "chapters",
                    "is_live",
                    "live_status",
                    "availability",
                )
            }
        else:
            files = [
                path
                for path in args.destination.glob("source.*")
                if path.suffix in (".mkv", ".mp4", ".webm", ".mov")
            ]
            if len(files) != 1:
                raise PermanentError("Pobieranie nie utworzyło jednoznacznego pliku źródłowego.")
            result = {"source": files[0].name}
        temporary = args.destination / f"{args.operation}.part.json"
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.destination / f"{args.operation}.json")
        return 0
    except DownloadError as exc:
        # Raw yt-dlp errors can contain request headers/URLs; expose only classified text.
        error = classify_error(str(exc))
    except (ResourceWait, PermanentError, TransientError) as exc:
        error = exc
    print(str(error))
    return (
        75 if isinstance(error, ResourceWait) else 70 if isinstance(error, TransientError) else 65
    )


if __name__ == "__main__":
    sys.exit(main())
