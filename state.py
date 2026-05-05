"""Persistent state for sync — tracks feeds and processed episode GUIDs."""

import copy
import json
from pathlib import Path

_STATE_FILE = Path(__file__).parent / "state.json"

_DEFAULT: dict = {
    "feeds": [
        {
            "feed_id": "6280366",
            "show_name": "The AI Daily Brief",
            "rss_url": None,  # resolved on first sync via PodcastIndex API
        }
    ],
    "processed": [],
}


def load() -> dict:
    if not _STATE_FILE.exists():
        return json.loads(json.dumps(_DEFAULT))
    with _STATE_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    for key, value in _DEFAULT.items():
        data.setdefault(key, copy.deepcopy(value))
    return data


def save(state: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def mark_processed(state: dict, guid: str) -> None:
    if guid not in state["processed"]:
        state["processed"].append(guid)
