"""Resolve Spotify episode metadata without auth using the oEmbed API."""

import re
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

SPOTIFY_OEMBED = "https://open.spotify.com/oembed"
SPOTIFY_EPISODE_PREFIX = "https://open.spotify.com/episode/"

_RETRYABLE = frozenset({"500", "502", "503", "504"})

_SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SpotifyTranscript/1.0)"}


def _is_retryable(exc: BaseException) -> bool:
    msg = str(exc)
    return any(code in msg for code in _RETRYABLE)


def extract_episode_id(url: str) -> str:
    match = re.search(r"episode/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError(f"Could not extract episode ID from URL: {url}")
    return match.group(1)


def _canonical_url(url: str) -> str:
    """Strip query params (e.g. ?si=...) — oEmbed doesn't need them."""
    episode_id = extract_episode_id(url)
    return f"{SPOTIFY_EPISODE_PREFIX}{episode_id}"


def _validate_spotify_url(url: str) -> None:
    if not url.startswith(SPOTIFY_EPISODE_PREFIX):
        raise ValueError(
            f"Expected a Spotify episode URL starting with {SPOTIFY_EPISODE_PREFIX!r}, got: {url!r}"
        )
    extract_episode_id(url)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
)
def _fetch_oembed(url: str) -> dict:
    resp = requests.get(SPOTIFY_OEMBED, params={"url": url}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _scrape_show_name(canonical_url: str) -> str:
    """
    Fallback: scrape the Spotify episode page to extract the show name
    from the og:description meta tag when oEmbed doesn't include it.
    og:description format: "<Show Name> · Episode · <date>"
    """
    try:
        resp = requests.get(canonical_url, headers=_SCRAPE_HEADERS, timeout=15)
        resp.raise_for_status()
        # og:description contains the show name before the first "·" or "Episode"
        m = re.search(r'og:description[^>]*content=["\']([^"\']+)["\']', resp.text)
        if m:
            desc = m.group(1)
            # Format is typically: "Show Name · Episode · <date>"
            show = re.split(r"\s*[·•]\s*", desc)[0].strip()
            if show:
                return show
    except Exception:
        pass
    return ""


def get_episode_metadata(url: str) -> dict:
    """Return title, show name, and episode URL from a Spotify episode URL."""
    _validate_spotify_url(url)
    episode_id = extract_episode_id(url)
    canonical = _canonical_url(url)

    data = _fetch_oembed(canonical)

    raw_title = data.get("title", "")
    provider = data.get("provider_name", "Spotify")

    if " | " in raw_title:
        episode_title, show_name = raw_title.split(" | ", 1)
    else:
        episode_title = raw_title
        # oEmbed didn't include the show name — scrape it from the page
        show_name = _scrape_show_name(canonical) or provider

    return {
        "episode_id": episode_id,
        "episode_title": episode_title.strip(),
        "show_name": show_name.strip(),
        "spotify_url": url,
    }
