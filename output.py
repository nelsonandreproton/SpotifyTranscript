"""Write the transcription as a markdown file in the Obsidian vault."""

import os
import re
from datetime import datetime, UTC
from pathlib import Path


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.lstrip(".")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]


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

    filename = _safe_filename(episode_title) + ".md"
    output_path = output_dir / filename

    # Guard against path traversal: resolved path must stay inside output_dir
    if not output_path.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError(f"Computed output path escapes the transcriptions directory: {output_path}")

    if output_path.exists():
        stem = _safe_filename(episode_title)
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
