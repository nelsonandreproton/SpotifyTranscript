#!/usr/bin/env python3
"""
SpotifyTranscript — sync new episodes for all tracked feeds.

Usage:
    python sync.py

Checks each configured feed for episodes not yet transcribed and processes them.
State is persisted in state.json (processed episode GUIDs).
"""

import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV_VARS = [
    "PODCASTINDEX_API_KEY",
    "PODCASTINDEX_API_SECRET",
    "OBSIDIAN_TRANSCRIPTIONS_PATH",
]

MAX_EPISODES_PER_FEED = 10


def _check_env() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        print(f"Error: missing required environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your values.")
        sys.exit(1)


from podcast_index import get_feed_url, get_recent_episodes, download_mp3
from transcriber import transcribe
from output import write_markdown
from state import load as load_state, save as save_state, mark_processed


def _process_episode(ep: dict, show_name: str) -> Path:
    """Download, transcribe and write one episode. Returns the output path."""
    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        download_mp3(ep["mp3_url"], tmp_path)
        size_mb = Path(tmp_path).stat().st_size / 1_048_576
        print(f"      Downloaded {size_mb:.1f} MB")

        print("      Transcribing...", flush=True)
        transcript = transcribe(tmp_path)
        print(f"      Characters: {len(transcript)}")

        return write_markdown(
            episode_title=ep["title"],
            show_name=show_name,
            spotify_url=ep["spotify_url"],
            pub_date=ep["pub_date"],
            transcript=transcript,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main() -> None:
    _check_env()
    state = load_state()

    total_new = 0
    total_failed = 0

    for feed in state["feeds"]:
        feed_id = feed["feed_id"]
        show_name = feed["show_name"]

        print(f"\n=== {show_name} (feed {feed_id}) ===")

        # Resolve RSS URL once and cache it in state
        if not feed.get("rss_url"):
            print("  Resolving RSS URL...")
            feed["rss_url"] = get_feed_url(feed_id)
            save_state(state)
            print(f"  RSS: {feed['rss_url']}")

        print(f"  Fetching last {MAX_EPISODES_PER_FEED} episodes...")
        episodes = get_recent_episodes(feed_id, MAX_EPISODES_PER_FEED)

        new_episodes = [ep for ep in episodes if ep["guid"] not in state["processed"]]

        if not new_episodes:
            print("  No new episodes.")
            continue

        if len(new_episodes) == MAX_EPISODES_PER_FEED:
            print(
                f"  WARNING: all {MAX_EPISODES_PER_FEED} fetched episodes are new — "
                "some older episodes may have been missed."
            )

        print(f"  {len(new_episodes)} new episode(s) to process:")
        for ep in new_episodes:
            print(f"    - {ep['title']}")

        # Process oldest-first so state is consistent if interrupted mid-batch
        for ep in reversed(new_episodes):
            print(f"\n  → {ep['title']}")
            print(f"    Published: {ep['pub_date']}")
            try:
                output_path = _process_episode(ep, show_name)
                mark_processed(state, ep["guid"])
                save_state(state)
                print(f"    ✓ Saved: {output_path.name}")
                total_new += 1
            except Exception as exc:
                print(f"    ✗ Failed: {exc}")
                total_failed += 1

    print(f"\n{'='*40}")
    print(f"Done. {total_new} new transcript(s) written, {total_failed} failed.")


if __name__ == "__main__":
    main()
