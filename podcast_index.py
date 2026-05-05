"""Find RSS feed and MP3 URL for a podcast episode via PodcastIndex API."""

import hashlib
import ipaddress
import os
import socket
import time
from datetime import datetime, UTC
from difflib import SequenceMatcher
from urllib.parse import urlparse

import feedparser
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

PODCASTINDEX_API = "https://api.podcastindex.org/api/1.0"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
RSS_FEED_MIN_SIMILARITY = 0.3
EPISODE_MIN_SIMILARITY = 0.4


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is not None and exc.response.status_code in {500, 502, 503, 504}
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return False


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


def _validate_url(url: str, label: str) -> None:
    """Validate URL scheme and block requests to private/loopback/link-local addresses."""
    if not url.startswith("https://") and not url.startswith("http://"):
        raise ValueError(f"{label} has an unexpected scheme (got {url!r}); only http/https allowed")
    hostname = urlparse(url).hostname or ""
    if not hostname:
        raise ValueError(f"{label} has no hostname: {url!r}")
    # Block bare IP literals that are private/loopback/link-local
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"{label} resolves to a disallowed address: {hostname!r}")
    except ValueError as exc:
        if "disallowed" in str(exc):
            raise
        # hostname is not a bare IP — resolve and check
        try:
            resolved = socket.getaddrinfo(hostname, None)
            for *_, addr in resolved:
                ip = ipaddress.ip_address(addr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError(
                        f"{label} hostname {hostname!r} resolves to a disallowed address: {addr[0]!r}"
                    )
        except socket.gaierror:
            pass  # DNS failure — let the downstream request fail naturally


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


def get_feed_url(feed_id: str) -> str:
    """Return the RSS URL for a known PodcastIndex feed ID."""
    data = _api_get("/podcasts/byfeedid", {"id": feed_id})
    feed = data.get("feed", {})
    url = feed.get("url")
    if not url:
        raise RuntimeError(f"PodcastIndex returned no URL for feed_id={feed_id!r}")
    _validate_url(url, "RSS feed URL")
    return url


def get_recent_episodes(feed_id: str, max_episodes: int = 10) -> list[dict]:
    """
    Return up to max_episodes recent episodes for a feed, each as a dict with:
      guid, title, mp3_url, pub_date, spotify_url (may be empty string)
    Ordered newest-first.
    """
    data = _api_get("/episodes/byfeedid", {"id": feed_id, "max": max_episodes})
    items = data.get("items", [])
    episodes = []
    for item in items:
        # Find audio URL — prefer enclosureUrl, fall back to link fields
        mp3_url = item.get("enclosureUrl", "")
        if not mp3_url:
            continue
        try:
            _validate_url(mp3_url, "MP3 URL")
        except ValueError:
            continue

        # Some feeds include a Spotify link in the episode's additional metadata
        spotify_url = ""
        for alt in item.get("alternateEnclosureList") or []:
            for src in alt.get("sources") or []:
                href = src.get("uri", "")
                if "spotify.com" in href:
                    spotify_url = href
                    break

        pretty = item.get("datePublishedPretty", "")
        if not pretty:
            ts = item.get("datePublished")
            pretty = (
                datetime.fromtimestamp(ts, tz=UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
                if ts
                else ""
            )

        guid = str(item.get("guid") or item.get("id") or "")
        if not guid:
            print(f"      WARNING: skipping episode with no guid/id: {item.get('title', '?')!r}")
            continue

        episodes.append(
            {
                "guid": guid,
                "title": item.get("title", ""),
                "mp3_url": mp3_url,
                "pub_date": pretty,
                "spotify_url": spotify_url,
            }
        )
    return episodes


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

    _validate_url(feed_url, "RSS feed URL")
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

    _validate_url(mp3_url, "MP3 URL")
    pub_date = best_entry.get("published", "")
    return mp3_url, pub_date


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
)
def download_mp3(mp3_url: str, dest_path: str) -> None:
    """Stream-download the MP3, writing to dest_path.

    Redirects are followed (CDN URLs redirect legitimately), but the final
    URL after all redirects is validated to block SSRF via open redirects.
    """
    with requests.get(mp3_url, stream=True, timeout=(10, 60)) as resp:
        resp.raise_for_status()
        # Validate the final URL after any redirects
        final_url = resp.url
        if final_url != mp3_url:
            _validate_url(final_url, "MP3 redirect destination")
        total = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"Download exceeded {MAX_DOWNLOAD_BYTES // 1_048_576} MB limit"
                    )
                f.write(chunk)
