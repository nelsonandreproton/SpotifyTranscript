"""Find RSS feed and MP3 URL for a podcast episode via PodcastIndex API."""

import hashlib
import os
import time
from difflib import SequenceMatcher

import feedparser
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

PODCASTINDEX_API = "https://api.podcastindex.org/api/1.0"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
RSS_FEED_MIN_SIMILARITY = 0.3
EPISODE_MIN_SIMILARITY = 0.4

_RETRYABLE = frozenset({"500", "502", "503", "504"})


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc)
    return any(code in msg for code in _RETRYABLE)


def _auth_headers() -> dict:
    api_key = os.environ["PODCASTINDEX_API_KEY"]
    api_secret = os.environ["PODCASTINDEX_API_SECRET"]
    epoch = int(time.time())
    # SHA-1 required by PodcastIndex API spec — upstream constraint
    hash_str = hashlib.sha1(f"{api_key}{api_secret}{epoch}".encode()).hexdigest()
    return {
        "X-Auth-Date": str(epoch),
        "X-Auth-Key": api_key,
        "Authorization": hash_str,
        "User-Agent": "SpotifyTranscript/1.0",
    }


def _validate_https_url(url: str, label: str) -> None:
    if not url.startswith("https://") and not url.startswith("http://"):
        raise ValueError(f"{label} has an unexpected scheme (got {url!r}); only http/https allowed")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
)
def _api_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{PODCASTINDEX_API}{path}",
        params=params,
        headers=_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_rss_feed(show_name: str) -> str:
    """Search PodcastIndex for the show and return its RSS feed URL."""
    data = _api_get("/search/byterm", {"q": show_name, "max": 5})
    feeds = data.get("feeds", [])
    if not feeds:
        raise RuntimeError(f"No podcast found for show: {show_name!r}")

    best = max(feeds, key=lambda f: _similarity(f.get("title", ""), show_name))
    score = _similarity(best.get("title", ""), show_name)
    if score < RSS_FEED_MIN_SIMILARITY:
        raise RuntimeError(
            f"No RSS feed matched {show_name!r} closely enough "
            f"(best: {best.get('title')!r}, score={score:.2f})"
        )

    feed_url = best.get("url")
    if not feed_url:
        raise RuntimeError("PodcastIndex returned a feed with no URL")

    _validate_https_url(feed_url, "RSS feed URL")
    return feed_url


def find_mp3_url(rss_url: str, episode_title: str) -> tuple[str, str]:
    """
    Parse the RSS feed and find the episode MP3 URL by matching title.
    Returns (mp3_url, pub_date).
    """
    feed = feedparser.parse(rss_url)

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            f"Failed to parse RSS feed {rss_url!r}: {feed.bozo_exception}"
        )

    if not feed.entries:
        raise RuntimeError(f"RSS feed has no entries: {rss_url}")

    best_entry = max(
        feed.entries,
        key=lambda e: _similarity(e.get("title", ""), episode_title),
    )

    score = _similarity(best_entry.get("title", ""), episode_title)
    if score < EPISODE_MIN_SIMILARITY:
        raise RuntimeError(
            f"Could not find episode {episode_title!r} in feed "
            f"(best match: {best_entry.get('title')!r}, score={score:.2f})"
        )

    mp3_url = None
    for link in best_entry.get("enclosures", []):
        if "audio" in link.get("type", "") or link.get("href", "").endswith(".mp3"):
            mp3_url = link["href"]
            break

    if not mp3_url:
        for link in best_entry.get("links", []):
            if "audio" in link.get("type", ""):
                mp3_url = link["href"]
                break

    if not mp3_url:
        raise RuntimeError(f"No audio enclosure found for episode: {episode_title!r}")

    _validate_https_url(mp3_url, "MP3 URL")
    pub_date = best_entry.get("published", "")
    return mp3_url, pub_date


def download_mp3(mp3_url: str, dest_fd: int) -> None:
    """Stream-download the MP3, writing to the open file descriptor dest_fd."""
    with requests.get(mp3_url, stream=True, timeout=(10, 60)) as resp:
        resp.raise_for_status()
        total = 0
        with os.fdopen(dest_fd, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"Download exceeded {MAX_DOWNLOAD_BYTES // 1_048_576} MB limit"
                    )
                f.write(chunk)
