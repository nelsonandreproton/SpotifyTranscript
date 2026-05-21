#!/usr/bin/env python3
"""
post_process.py — Post-processing pipeline for SpotifyTranscript.

For each transcription .md in OBSIDIAN_TRANSCRIPTIONS_PATH that lacks
`summarized: true` in its frontmatter:
  1. Generate a 20-point bullet summary via LLM (NIM or local Qwen).
  2. Insert a ## Summary section into the markdown file.
  3. Mark the file as summarized (frontmatter flag).
  4. For "The AI Daily Brief" episodes: insert a new card into the HTML mindmap.
"""

from __future__ import annotations

import html as _html
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, UTC
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SHOW_AI_DAILY_BRIEF = "The AI Daily Brief"
MAX_TRANSCRIPT_CHARS = 24_000

# Known theme sections in the HTML, in document order.
# LLM picks the best-matching keyword; we use it to locate the insertion point.
THEMES = [
    ("modelos", "🤖 Modelos"),
    ("negocio", "💰 Modelo de Negócio"),
    ("trabalho", "👥 Trabalho"),
    ("infraestrutura", "🏗️ Infraestrutura"),
    ("sociedade", "🌐 Sociedade"),
]

# ── YAML frontmatter helpers ─────────────────────────────────────────────────

_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). frontmatter_block includes delimiters."""
    m = _FM_PATTERN.match(text)
    if not m:
        return ("", text)
    return (m.group(0), text[m.end():])


def _get_fm_value(frontmatter: str, key: str) -> str:
    """Extract a scalar value from raw YAML frontmatter text."""
    pattern = re.compile(rf'^{re.escape(key)}:\s*"?(.*?)"?\s*$', re.MULTILINE)
    m = pattern.search(frontmatter)
    return m.group(1).strip('"') if m else ""


def _set_fm_flag(frontmatter: str, key: str, value: str) -> str:
    """Add or update a scalar flag in raw YAML frontmatter text."""
    pattern = re.compile(rf'^{re.escape(key)}:.*$', re.MULTILINE)
    if pattern.search(frontmatter):
        return pattern.sub(f'{key}: {value}', frontmatter)
    # Insert before closing ---
    inner = re.sub(r'\n---\n$', f'\n{key}: {value}\n---\n', frontmatter)
    return inner


def _is_summarized(frontmatter: str) -> bool:
    return _get_fm_value(frontmatter, "summarized").lower() == "true"


# ── Markdown summary insertion ───────────────────────────────────────────────

def _insert_summary(body: str, summary: str) -> str:
    """
    Insert ## Summary section before ## Transcript.
    If ## Summary already exists, replace it.
    """
    summary_block = f"## Summary\n\n{summary.strip()}\n\n"

    # Replace existing summary block
    existing = re.compile(r'^## Summary\n.*?(?=^## |\Z)', re.MULTILINE | re.DOTALL)
    if existing.search(body):
        return existing.sub(summary_block, body, count=1)

    # Insert before ## Transcript
    transcript_marker = re.compile(r'^## Transcript', re.MULTILINE)
    m = transcript_marker.search(body)
    if m:
        return body[:m.start()] + summary_block + body[m.start():]

    # Fallback: append
    return body + "\n\n" + summary_block


# ── LLM calls ────────────────────────────────────────────────────────────────

def _build_summary_messages(title: str, transcript: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a precise podcast summarizer. "
                "Follow the user's format exactly. Be factual and concise."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Summarize the following podcast transcript titled \"{title}\" "
                f"as exactly 20 numbered bullet points. "
                f"Each point must be one concise sentence capturing a distinct key insight. "
                f"Use the format:\n1. ...\n2. ...\n...\n20. ...\n\n"
                f"TRANSCRIPT:\n{transcript}"
            ),
        },
    ]


_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_keyword": {
            "type": "string",
            "enum": [t[0] for t in THEMES],
            "description": "Best-matching theme keyword from the list.",
        },
        "date_pt": {
            "type": "string",
            "description": "Publication date in Portuguese format, e.g. '07 Mai 2026'.",
        },
        "title": {"type": "string"},
        "summary_pt": {
            "type": "string",
            "description": "1–2 sentence summary in Portuguese.",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
            "description": "2–4 short key facts in Portuguese.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 4,
        },
    },
    "required": ["theme_keyword", "date_pt", "title", "summary_pt", "key_points", "tags"],
}

_THEME_DESCRIPTIONS = "\n".join(f"  - {kw}: {label}" for kw, label in THEMES)


def _build_card_messages(title: str, pub_date: str, summary_bullets: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a precise data extractor. "
                "Output only valid JSON matching the requested schema."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Given this podcast episode summary, produce a JSON card for an HTML mindmap.\n\n"
                f"Episode title: {title}\n"
                f"Published: {pub_date}\n\n"
                f"Summary:\n{summary_bullets}\n\n"
                f"Available themes (pick the best match for theme_keyword):\n{_THEME_DESCRIPTIONS}\n\n"
                f"Rules:\n"
                f"- date_pt: convert '{pub_date}' to Portuguese abbreviated month format, e.g. '07 Mai 2026'\n"
                f"- summary_pt: 1–2 sentences in European Portuguese\n"
                f"- key_points: 2–4 items in European Portuguese, each under 80 chars\n"
                f"- tags: 1–4 short English or Portuguese topic tags\n"
            ),
        },
    ]


# ── HTML card insertion ──────────────────────────────────────────────────────

_ACCENT_COLORS = {
    "modelos": "#00f2fe",
    "negocio": "#ffd700",
    "trabalho": "#ff7eb3",
    "infraestrutura": "#43e97b",
    "sociedade": "#fa709a",
}


def _render_card_html(card: dict) -> str:
    accent = _ACCENT_COLORS.get(card["theme_keyword"], "#aaaaaa")
    theme_kw = _html.escape(card["theme_keyword"], quote=True)
    tags_html = "".join(
        f'<span class="tag">{_html.escape(t)}</span>' for t in card["tags"]
    )
    points_html = "".join(
        f"      <li>{_html.escape(p)}</li>\n" for p in card["key_points"]
    )
    return (
        f'\n  <div class="article-card" data-tags="{theme_kw}" style="--accent:{accent}">\n'
        f'    <div class="article-date">{_html.escape(card["date_pt"])}</div>\n'
        f'    <h4 class="article-title">{_html.escape(card["title"])}</h4>\n'
        f'    <p class="article-summary">{_html.escape(card["summary_pt"])}</p>\n'
        f'    <ul class="key-points">\n'
        f'{points_html}'
        f'    </ul>\n'
        f'    <div class="tags">{tags_html}</div>\n'
        f'  </div>\n'
    )


def _ensure_data_theme_attrs(html: str) -> str:
    """
    Add data-theme="<kw>" to any theme-section div that doesn't already have one.
    Matches by the display label text inside the <h3>.
    """
    for kw, label in THEMES:
        label_text = label.split(" ", 1)[-1] if " " in label else label
        # Find theme-section divs that contain this label in their h3 but lack data-theme
        pattern = re.compile(
            r'(<div class="theme-section")([^>]*?>[\s\S]*?<h3>[^<]*'
            + re.escape(label_text.split("&")[0].strip())
            + r')',
        )
        def _add_attr(m: re.Match, _kw: str = kw) -> str:
            opening_tag = m.group(1)
            rest = m.group(2)
            if f'data-theme="{_kw}"' in opening_tag:
                return m.group(0)
            return opening_tag + f' data-theme="{_kw}"' + rest
        html = pattern.sub(_add_attr, html, count=1)
    return html


def _insert_card_into_html(html_path: Path, card: dict) -> None:
    """Insert a new article-card into the correct theme section of the HTML mindmap."""
    html = html_path.read_text(encoding="utf-8")

    # Ensure theme-sections have data-theme attributes for reliable matching
    html = _ensure_data_theme_attrs(html)

    theme_kw = card["theme_keyword"]
    card_html = _render_card_html(card)

    # Find the theme-section with data-theme="<theme_kw>"
    section_pattern = re.compile(
        rf'<div class="theme-section"[^>]*\bdata-theme="{re.escape(theme_kw)}"'
    )
    m = section_pattern.search(html)

    if m is None:
        # Fallback: append before </body>
        html = html.replace("</body>", card_html + "</body>")
    else:
        # Insert before the next theme-section (or before </body> if this is the last)
        next_section = re.search(r'<div class="theme-section"', html[m.end():])
        if next_section:
            insert_at = m.end() + next_section.start()
        else:
            insert_at = html.rfind("</body>")
            if insert_at == -1:
                insert_at = len(html)
        html = html[:insert_at] + card_html + html[insert_at:]

    # Write backup then atomically update
    shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
    tmp = html_path.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(html_path)


# ── File processing ──────────────────────────────────────────────────────────

def _clip_transcript(transcript: str) -> str:
    """Take the last MAX_TRANSCRIPT_CHARS characters (preserves deep-dive section)."""
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        clipped = len(transcript) - MAX_TRANSCRIPT_CHARS
        print(f"      Transcript clipped: {clipped} chars removed from start")
        return transcript[-MAX_TRANSCRIPT_CHARS:]
    return transcript


def process_file(md_path: Path, html_path: Path | None) -> None:
    """Summarize one transcription file and optionally update the HTML mindmap."""
    import llm

    text = md_path.read_text(encoding="utf-8")
    fm_block, body = _split_frontmatter(text)

    if not fm_block:
        print(f"  [skip] No frontmatter: {md_path.name}")
        return

    if _is_summarized(fm_block):
        print(f"  [skip] Already summarized: {md_path.name}")
        return

    title = _get_fm_value(fm_block, "title") or md_path.stem
    show = _get_fm_value(fm_block, "show")
    pub_date = _get_fm_value(fm_block, "published")

    print(f"\n  → {md_path.name}")
    print(f"    Title : {title}")
    print(f"    Show  : {show}")

    # Extract transcript section
    transcript_match = re.search(r'^## Transcript\n+(.*)', body, re.DOTALL | re.MULTILINE)
    transcript = transcript_match.group(1).strip() if transcript_match else body.strip()
    transcript = _clip_transcript(transcript)

    # Step 1: Generate 20-point summary
    print("    Generating summary...", flush=True)
    messages = _build_summary_messages(title, transcript)
    summary = llm.chat(messages, max_tokens=1024, temperature=0.1)
    print(f"    Summary: {len(summary)} chars")

    # Step 2: Insert ## Summary into markdown (atomic write)
    new_body = _insert_summary(body, summary)
    new_fm = _set_fm_flag(fm_block, "summarized", "true")
    tmp_md = md_path.with_suffix(".md.tmp")
    tmp_md.write_text(new_fm + new_body, encoding="utf-8")
    tmp_md.replace(md_path)
    print(f"    ✓ Summary written to {md_path.name}")

    # Step 3: HTML card for AI Daily Brief only
    if show == SHOW_AI_DAILY_BRIEF and html_path and html_path.exists():
        print("    Generating HTML card...", flush=True)
        card_messages = _build_card_messages(title, pub_date, summary)
        card = llm.chat_json(card_messages, schema=_CARD_SCHEMA, max_tokens=512, temperature=0.1)
        _insert_card_into_html(html_path, card)
        print(f"    ✓ Card inserted into HTML (theme: {card['theme_keyword']})")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    transcriptions_path = os.environ.get("OBSIDIAN_TRANSCRIPTIONS_PATH", "")
    if not transcriptions_path:
        print("Error: OBSIDIAN_TRANSCRIPTIONS_PATH not set in .env")
        sys.exit(1)

    transcriptions_dir = Path(transcriptions_path)
    if not transcriptions_dir.exists():
        print(f"Error: transcriptions directory not found: {transcriptions_dir}")
        sys.exit(1)

    html_path = transcriptions_dir / "AI Daily Brief - Mapa Mental.html"

    md_files = sorted(transcriptions_dir.glob("*.md"))
    if not md_files:
        print("No transcription files found.")
        return

    print(f"Scanning {len(md_files)} file(s) in {transcriptions_dir}")

    processed = 0
    failed = 0
    for md_path in md_files:
        try:
            process_file(md_path, html_path if html_path.exists() else None)
            processed += 1
        except Exception as exc:
            print(f"  ✗ Failed {md_path.name}: {exc}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Done. {processed} file(s) processed, {failed} failed.")
    print(f"Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")


if __name__ == "__main__":
    main()
