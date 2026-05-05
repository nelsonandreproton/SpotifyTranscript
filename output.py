"""Write the transcription as a markdown file in the Obsidian vault."""

import os
import re
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def _safe_filename(name: str) -> str:
    name = name.replace("\x00", "")          # strip null bytes
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.lstrip(".")
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:200]
    # Avoid Windows reserved device names (with or without extension)
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def _pub_date_prefix(pub_date: str) -> str:
    """Return YYYY-MM-DD prefix from a pub_date string, or empty string on failure.

    Handles:
    - RFC 2822: "Fri, 01 May 2026 20:45:04 GMT"  (from RSS feed / transcribe.py)
    - PodcastIndex pretty: "May 01, 2026 3:45pm"  (from sync.py via API)
    """
    if not pub_date:
        return ""
    # Try RFC 2822 first (email.utils handles timezone variations)
    try:
        return parsedate_to_datetime(pub_date).strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try PodcastIndex pretty format: "Month DD, YYYY H:MMam/pm"
    for fmt in ("%B %d, %Y %I:%M%p", "%B %d, %Y %I%p"):
        try:
            return datetime.strptime(pub_date.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_HR = "\n\n---\n\n"


def _mark_sponsor_boundaries(text: str) -> str:
    if "Next up, the main episode." in text:
        text = text.replace("Next up, the main episode.", "Next up, the main episode." + _HR, 1)
    if "Welcome back to the AI Daily Brief." in text:
        text = text.replace("Welcome back to the AI Daily Brief.", _HR + "Welcome back to the AI Daily Brief.", 1)
    return text


def _wrap_transcript(text: str) -> str:
    """Break long transcript into readable paragraphs (~5 sentences each)."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    paragraphs = []
    chunk = []
    for i, sentence in enumerate(sentences):
        chunk.append(sentence)
        if (i + 1) % 5 == 0:
            paragraphs.append(" ".join(chunk))
            chunk = []
    if chunk:
        paragraphs.append(" ".join(chunk))
    return "\n\n".join(paragraphs)


def write_markdown(
    episode_title: str,
    show_name: str,
    spotify_url: str,
    pub_date: str,
    transcript: str,
) -> Path:
    """Write transcript to Obsidian vault and return the file path."""
    output_dir = Path(os.environ["OBSIDIAN_TRANSCRIPTIONS_PATH"])
    output_dir.mkdir(parents=True, exist_ok=True)

    date_prefix = _pub_date_prefix(pub_date)
    safe_title = _safe_filename(episode_title)
    stem = f"{date_prefix} {safe_title}" if date_prefix else safe_title
    filename = stem + ".md"
    output_path = output_dir / filename

    # Guard against path traversal: resolved path must stay inside output_dir
    if not output_path.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError(f"Computed output path escapes the transcriptions directory: {output_path}")

    if output_path.exists():
        suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        output_path = output_dir / f"{stem}_{suffix}.md"

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    content = f"""---
title: "{_yaml_escape(episode_title)}"
show: "{_yaml_escape(show_name)}"
spotify_url: "{_yaml_escape(spotify_url)}"
published: "{_yaml_escape(pub_date)}"
transcribed_at: "{generated_at}"
tags:
  - podcast
  - transcript
---

# {episode_title}

**Show:** {show_name}
**Spotify:** [{spotify_url}]({spotify_url})
**Published:** {pub_date}

---

## Transcript

{_wrap_transcript(_mark_sponsor_boundaries(transcript))}
"""

    output_path.write_text(content, encoding="utf-8")
    return output_path
