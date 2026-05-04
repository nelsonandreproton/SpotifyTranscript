#!/usr/bin/env python3
"""
SpotifyTranscript — transcribe a Spotify podcast episode to markdown.

Usage:
    python transcribe.py <spotify_episode_url>

Example:
    python transcribe.py https://open.spotify.com/episode/21uKecB3Xvxu4nHiOLThFA
"""

import sys
import tempfile
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV_VARS = [
    "PODCASTINDEX_API_KEY",
    "PODCASTINDEX_API_SECRET",
    "OBSIDIAN_TRANSCRIPTIONS_PATH",
]


def _check_env() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print(f"Error: missing required environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your values.")
        sys.exit(1)


from spotify import get_episode_metadata
from podcast_index import find_rss_feed, find_mp3_url, download_mp3
from transcriber import transcribe
from output import write_markdown


def main(spotify_url: str) -> None:
    _check_env()

    print("\n=== SpotifyTranscript ===")
    print(f"URL: {spotify_url}\n")

    # 1. Resolve Spotify metadata
    print("[1/5] Resolving episode metadata from Spotify...")
    meta = get_episode_metadata(spotify_url)
    print(f"      Episode : {meta['episode_title']}")
    print(f"      Show    : {meta['show_name']}")

    # 2. Find RSS feed
    print("\n[2/5] Searching PodcastIndex for RSS feed...")
    rss_url = find_rss_feed(meta["show_name"])
    print(f"      RSS     : {rss_url}")

    # 3. Find MP3 URL in feed
    print("\n[3/5] Locating episode in RSS feed...")
    mp3_url, pub_date = find_mp3_url(rss_url, meta["episode_title"])
    print(f"      MP3     : {mp3_url}")
    print(f"      Date    : {pub_date}")

    # 4. Download MP3 — keep fd open while writing to avoid TOCTOU
    print("\n[4/5] Downloading audio...")
    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    try:
        download_mp3(mp3_url, fd)  # download_mp3 closes fd via os.fdopen
        size_mb = Path(tmp_path).stat().st_size / 1_048_576
        print(f"      Downloaded {size_mb:.1f} MB")

        # 5. Transcribe
        print("\n[5/5] Transcribing (this may take a few minutes)...")
        transcript = transcribe(tmp_path)
        print(f"      Transcript length: {len(transcript)} characters")

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # 6. Write markdown
    output_path = write_markdown(
        episode_title=meta["episode_title"],
        show_name=meta["show_name"],
        spotify_url=spotify_url,
        pub_date=pub_date,
        transcript=transcript,
    )

    print(f"\n✓ Done! Transcript saved to:\n  {output_path}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python transcribe.py <spotify_episode_url>")
        sys.exit(1)
    main(sys.argv[1])
