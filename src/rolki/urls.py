import re
from urllib.parse import parse_qs, urlsplit

LINK = re.compile(r"https?://[^\s<>()\[\]]+", re.IGNORECASE)
VIDEO_ID = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def youtube_links(message: str) -> list[tuple[str, str]]:
    """Normalize only supported video URLs; never pass arbitrary URLs to yt-dlp."""
    found = {}
    for candidate in LINK.findall(message):
        parsed = urlsplit(candidate.rstrip(".,;!?)\"'"))
        host = (parsed.hostname or "").lower()
        try:
            if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                continue
        except ValueError:
            continue
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/shorts/"):
                video_id = parsed.path.removeprefix("/shorts/").strip("/")
        if VIDEO_ID.fullmatch(video_id):
            found[video_id] = f"https://www.youtube.com/watch?v={video_id}"
    return list(found.items())
