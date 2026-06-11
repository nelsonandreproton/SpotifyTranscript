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

import copy
import html as _html
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, UTC
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SHOW_AI_DAILY_BRIEF = "The AI Daily Brief"
MAX_TRANSCRIPT_CHARS = 24_000

# Known theme sections in the HTML, in document order.
# LLM picks the best-matching keyword; we use it to locate the insertion point.
THEMES = [
    ("modelos", "🤖 Models & Harnesses"),
    ("negocio", "💰 Business Model"),
    ("trabalho", "👥 Work & Jobs"),
    ("infraestrutura", "🏗️ Infrastructure"),
    ("sociedade", "🌐 Society & Policy"),
]

# Short description shown under each theme heading in the HTML.
THEME_DESCS = {
    "modelos": "The competition has shifted: it's now more about the environment around the model than the model itself.",
    "negocio": "How AI is reshaping business models, pricing, and competitive dynamics across the industry.",
    "trabalho": "The ways AI is changing how we work, hire, and think about productivity and career paths.",
    "infraestrutura": "Compute, energy, data-center build-out, and the infrastructure race underpinning the AI boom.",
    "sociedade": "Policy, safety, public perception, and the broader societal implications of rapid AI deployment.",
}

# Central actors tracked across episodes.
ACTORS = ["OpenAI", "Anthropic", "Google", "Meta", "Atlassian", "xAI/SpaceX"]
# Substring keywords used to auto-detect actors in card text (lower-cased).
_ACTOR_KEYWORDS: dict[str, list[str]] = {
    "OpenAI": ["openai", "gpt", "codex", "sam altman", "chatgpt"],
    "Anthropic": ["anthropic", "claude", "dario"],
    "Google": ["google", "gemini", "deepmind"],
    "Meta": ["meta", "llama", "zuckerberg"],
    "Atlassian": ["atlassian", "rovo", "cannon-brookes"],
    "xAI/SpaceX": ["xai", "spacex", "elon", "grok", "colossus", "terrafab"],
}

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


def _is_card_english(frontmatter: str) -> bool:
    return _get_fm_value(frontmatter, "card_english").lower() == "true"


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
        "date_en": {
            "type": "string",
            "description": "Publication date in English format, e.g. 'May 7, 2026'.",
        },
        "title": {"type": "string"},
        "summary_en": {
            "type": "string",
            "description": "1–2 sentence summary in English.",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
            "description": "2–4 short key facts in English, each under 80 chars.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 4,
            "description": "1–4 short English topic tags.",
        },
    },
    "required": ["theme_keyword", "date_en", "title", "summary_en", "key_points", "tags"],
}


def _detect_actors(text: str) -> list[str]:
    """Return list of ACTORS mentioned in text (case-insensitive keyword match)."""
    lower = text.lower()
    return [actor for actor, keywords in _ACTOR_KEYWORDS.items()
            if any(kw in lower for kw in keywords)]


def _pub_date_to_iso(pub_date: str) -> str:
    """Convert various date formats to ISO '2026-05-21'. Returns '' on failure.

    Handles:
      'May 21, 2026 4:47pm'   (PodcastIndex pretty format)
      'May 21, 2026'
      'Thu, 30 Apr 2026 20:00:00 GMT'  (RSS/RFC 2822)
    """
    s = pub_date.strip()
    for fmt in (
        "%B %d, %Y %I:%M%p",
        "%B %d, %Y %I:%M %p",
        "%B %d, %Y",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Last-resort: grab first 3 tokens that look like "Mon DD YYYY" or "DD Mon YYYY"
    parts = s.split()
    for start in range(len(parts) - 2):
        for fmt in ("%d %b %Y", "%B %d %Y"):
            try:
                return datetime.strptime(" ".join(parts[start:start + 3]), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""

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
                f"- date_en: convert '{pub_date}' to English format, e.g. 'May 7, 2026'\n"
                f"- summary_en: 1–2 sentences in English\n"
                f"- key_points: 2–4 items in English, each under 80 chars\n"
                f"- tags: 1–4 short English topic tags\n"
            ),
        },
    ]


# ── Personal takeaways (Nelson-specific) ─────────────────────────────────────

_PERSONAL_TAKEAWAYS_SCHEMA = {
    "type": "object",
    "properties": {
        "takeaways": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "insight": {
                        "type": "string",
                        "description": "One-sentence insight drawn from the summaries, under 140 chars.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Name of ONE specific project from Nelson's list this applies to.",
                    },
                    "action": {
                        "type": "string",
                        "description": "One concrete action Nelson can take this week, under 140 chars.",
                    },
                },
                "required": ["insight", "project", "action"],
            },
        },
    },
    "required": ["takeaways"],
}


def _build_personal_takeaways_messages(me_md: str, summaries_blob: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You produce highly personalized, actionable takeaways for Nelson "
                "from a corpus of AI podcast summaries. You output ONLY valid JSON "
                "matching the requested schema. Every takeaway must satisfy ALL "
                "criteria in Nelson's 'What Good Insights Look Like' rubric: "
                "(1) applies to a specific active project or Claude Code setup, "
                "(2) actionable this week, (3) challenges current architecture or "
                "assumptions, (4) specific to Python/Telegram/Docker/AI tooling, "
                "(5) helps him think about AI systems, not just AI features. "
                "Reject generic advice. Each takeaway must name ONE specific project."
            ),
        },
        {
            "role": "user",
            "content": (
                "Nelson's personal profile (me.md):\n"
                "================================\n"
                f"{me_md}\n"
                "================================\n\n"
                "Aggregated summaries of recent AI Daily Brief episodes:\n"
                "================================\n"
                f"{summaries_blob}\n"
                "================================\n\n"
                "Produce exactly 5 takeaways. Each must:\n"
                "- Name ONE specific project from Nelson's active list "
                "(GarminBot, Xread, CNCSearch, NPChat, HetznerCheck, JMJ2027, "
                "CienciaViva, JoaoAlmeidaTracker, PTStorms, Nektar, LiturgiaDasHoras, "
                "canticos_site_flask, homeserver, obsidian-second-brain, "
                "FinancialTracker, SpotifyTranscript, Whatsapp-Send-Message, "
                "PTEvents, PTSquawk, MyClaw, Andre) "
                "or his Claude Code setup.\n"
                "- Tie the insight to a specific idea from the summaries.\n"
                "- Give a concrete action Nelson can do this week.\n"
                "- Avoid platitudes like 'consider AI' or 'AI is changing work'."
            ),
        },
    ]


def _collect_summaries_blob(md_dir: Path, max_chars: int = 60_000) -> str:
    """Concatenate all transcription summaries into a single blob for LLM context."""
    parts: list[str] = []
    md_files = sorted(md_dir.glob("*.md"), reverse=True)  # newest first
    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = _split_frontmatter(text)
        if not fm:
            continue
        title = _get_fm_value(fm, "title") or md_path.stem
        m = re.search(r'^## Summary\n+(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
        if not m:
            continue
        summary_text = m.group(1).strip()
        parts.append(f"### {title}\n{summary_text}")
    blob = "\n\n".join(parts)
    if len(blob) > max_chars:
        blob = blob[:max_chars]
    return blob


def _find_me_md(md_dir: Path) -> Path | None:
    """Locate me.md in the Obsidian vault root.

    Transcriptions live at <vault>/projects/SpotifyTranscript/Transcriptions/,
    so vault root is md_dir.parents[2]. Allow override via env.
    """
    override = os.environ.get("OBSIDIAN_ME_PATH", "").strip()
    if override:
        p = Path(override)
        if p.exists():
            return p
    try:
        candidate = md_dir.parents[2] / "me.md"
        if candidate.exists():
            return candidate
    except IndexError:
        pass
    return None


def _generate_personal_takeaways(md_dir: Path) -> list[dict] | None:
    """Generate 5 Nelson-specific takeaways from me.md + all summaries.

    Returns None if me.md is missing, no summaries exist, or the LLM fails.
    """
    me_path = _find_me_md(md_dir)
    if me_path is None:
        print("  [takeaways] me.md not found — skipping personal takeaways section")
        return None
    try:
        me_md = me_path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  [takeaways] failed to read me.md: {exc}")
        return None

    summaries_blob = _collect_summaries_blob(md_dir)
    if not summaries_blob:
        print("  [takeaways] no summaries found — skipping personal takeaways section")
        return None

    import llm
    try:
        messages = _build_personal_takeaways_messages(me_md, summaries_blob)
        result = llm.chat_json(
            messages,
            schema=_PERSONAL_TAKEAWAYS_SCHEMA,
            max_tokens=1500,
            temperature=0.2,
        )
    except Exception as exc:
        print(f"  [takeaways] LLM call failed: {exc}")
        return None

    takeaways = result.get("takeaways") if isinstance(result, dict) else None
    if not takeaways or not isinstance(takeaways, list):
        print("  [takeaways] LLM returned no takeaways")
        return None
    return takeaways[:5]


def _inject_personal_takeaways_section(soup, takeaways: list[dict]) -> None:
    """Insert (or replace) the .personal-takeaways section right after .central-node."""
    from bs4 import BeautifulSoup as _BS

    for existing in soup.find_all("div", class_="personal-takeaways"):
        existing.decompose()

    central = soup.find("div", class_="central-node")
    if central is None:
        return

    cards_html_parts: list[str] = []
    for idx, t in enumerate(takeaways, start=1):
        insight = _html.escape(str(t.get("insight", "")))
        project = _html.escape(str(t.get("project", "")))
        action = _html.escape(str(t.get("action", "")))
        cards_html_parts.append(
            '<div class="takeaway-card">'
            f'<div class="takeaway-num">{idx:02d}</div>'
            '<div class="takeaway-body">'
            f'<p class="takeaway-insight">{insight}</p>'
            '<div class="takeaway-meta">'
            f'<span class="takeaway-project">{project}</span>'
            f'<span class="takeaway-action">→ {action}</span>'
            '</div>'
            '</div>'
            '</div>'
        )
    cards_html = "".join(cards_html_parts)

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    section_html = (
        '<div class="personal-takeaways">'
        '<div class="takeaways-header">'
        '<h3>⚡ Personal Takeaways for Nelson</h3>'
        f'<span class="takeaways-stamp">updated {timestamp}</span>'
        '</div>'
        f'<div class="takeaways-grid">{cards_html}</div>'
        '</div>'
    )
    new_section = _BS(section_html, "html.parser").find("div", class_="personal-takeaways")
    central.insert_after(new_section)


_PERSONAL_TAKEAWAYS_CSS = """
  /* ── Personal Takeaways ──────────────────────────────────── */
  .personal-takeaways {
    grid-column: 1 / -1;
    margin-top: 18px;
    padding: 24px;
    border-radius: 14px;
    background: linear-gradient(135deg, rgba(124,58,237,0.18), rgba(0,242,254,0.10));
    border: 1px solid rgba(255,255,255,0.12);
    box-shadow: 0 8px 28px rgba(0,0,0,0.35);
  }
  .takeaways-header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 16px;
  }
  .takeaways-header h3 {
    font-size: 1.25em;
    background: linear-gradient(90deg, #00f2fe, #b465da);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .takeaways-stamp { font-size: 0.72em; color: #888; letter-spacing: 0.5px; text-transform: uppercase; }
  .takeaways-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }
  .takeaway-card {
    display: flex; gap: 12px;
    background: rgba(0,0,0,0.25);
    border: 1px solid rgba(255,255,255,0.08);
    border-left: 3px solid #b465da;
    border-radius: 10px;
    padding: 14px 16px;
    transition: transform 0.2s, border-color 0.2s;
  }
  .takeaway-card:hover { transform: translateY(-2px); border-left-color: #00f2fe; }
  .takeaway-num {
    font-size: 1.4em; font-weight: 700;
    background: linear-gradient(135deg, #b465da, #00f2fe);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
    flex-shrink: 0; line-height: 1;
  }
  .takeaway-body { flex: 1; min-width: 0; }
  .takeaway-insight { font-size: 0.9em; color: #e8e8e8; line-height: 1.45; margin-bottom: 8px; }
  .takeaway-meta { display: flex; flex-direction: column; gap: 4px; }
  .takeaway-project {
    display: inline-block; align-self: flex-start;
    font-size: 0.7em; font-weight: 600; letter-spacing: 0.5px;
    color: #00f2fe; text-transform: uppercase;
    padding: 2px 8px; border-radius: 10px;
    background: rgba(0,242,254,0.10); border: 1px solid rgba(0,242,254,0.25);
  }
  .takeaway-action { font-size: 0.78em; color: #b8b8b8; line-height: 1.4; }
"""


def _ensure_personal_takeaways_css(soup) -> None:
    """Append the Personal Takeaways CSS block to <style> if not already present."""
    style_tag = soup.find("style")
    if style_tag and style_tag.string and "personal-takeaways" not in style_tag.string:
        style_tag.string = style_tag.string + _PERSONAL_TAKEAWAYS_CSS


# ── HTML card insertion ──────────────────────────────────────────────────────

_ACCENT_COLORS = {
    "modelos": "#00f2fe",
    "negocio": "#ffd700",
    "trabalho": "#ff7eb3",
    "infraestrutura": "#43e97b",
    "sociedade": "#fa709a",
}


def _obsidian_uri(md_filename: str) -> str:
    """Return obsidian://open URI for a transcription md file."""
    note_path = f"projects/SpotifyTranscript/Transcriptions/{md_filename}"
    from urllib.parse import quote
    return f"obsidian://open?vault=Nelson&file={quote(note_path)}"


def _render_card_html(card: dict) -> str:
    accent = _ACCENT_COLORS.get(card["theme_keyword"], "#aaaaaa")
    theme_kw = _html.escape(card["theme_keyword"], quote=True)
    actors = card.get("actors", [])
    actors_attr = _html.escape(",".join(actors), quote=True)
    iso_date = card.get("iso_date", "")
    summary_full = _html.escape(card.get("summary_full", ""), quote=True)
    obsidian_uri = _html.escape(card.get("obsidian_uri", ""), quote=True)
    tags_html = "".join(
        f'<span class="tag">{_html.escape(t)}</span>' for t in card["tags"]
    )
    points_html = "".join(
        f"      <li>{_html.escape(p)}</li>\n" for p in card["key_points"]
    )
    return (
        f'\n  <div class="article-card" data-tags="{theme_kw}"'
        f' data-actors="{actors_attr}" data-date="{iso_date}"'
        f' data-summary="{summary_full}" data-obsidian="{obsidian_uri}"'
        f' style="--accent:{accent}">\n'
        f'    <div class="article-date">{_html.escape(card.get("date_en", card.get("date_pt", "")))}</div>\n'
        f'    <h4 class="article-title">{_html.escape(card["title"])}</h4>\n'
        f'    <p class="article-summary">{_html.escape(card.get("summary_en", card.get("summary_pt", "")))}</p>\n'
        f'    <ul class="key-points">\n'
        f'{points_html}'
        f'    </ul>\n'
        f'    <div class="tags">{tags_html}</div>\n'
        f'  </div>\n'
    )


def _ensure_data_theme_attrs(html: str) -> str:
    """
    Ensure each theme-section div has exactly one correct data-theme attribute.
    Matches each section individually by its h3 label text.
    Strips duplicate data-theme attrs accumulated by previous buggy runs.
    """
    for kw, label in THEMES:
        label_keyword = label.split(" ", 1)[-1].split("&")[0].strip() if " " in label else label

        # Match only the opening tag of this specific theme-section.
        # Strategy: find the opening tag whose following h3 text contains the label keyword.
        # We do this section-by-section using a two-pass approach:
        #   1. Locate the <h3> for this section.
        #   2. Walk backwards to find its parent theme-section opening tag.
        h3_pattern = re.compile(
            r'<h3>[^<]*' + re.escape(label_keyword) + r'[^<]*</h3>'
        )
        h3_match = h3_pattern.search(html)
        if not h3_match:
            continue

        # Find the last <div class="theme-section"...> before this <h3>
        before_h3 = html[:h3_match.start()]
        opening_pattern = re.compile(r'<div class="theme-section"[^>]*>')
        openings = list(opening_pattern.finditer(before_h3))
        if not openings:
            continue
        last_opening = openings[-1]
        tag_text = last_opening.group(0)

        # Build clean replacement: strip ALL data-theme attrs, add one correct one
        clean_tag = re.sub(r'\s*data-theme="[^"]*"', '', tag_text)
        # Insert data-theme after class attribute
        new_tag = clean_tag.replace(
            'class="theme-section"',
            f'class="theme-section" data-theme="{kw}"',
            1,
        )
        html = html[:last_opening.start()] + new_tag + html[last_opening.end():]

    return html


def _card_already_in_html(html: str, title: str) -> bool:
    """Return True if a card with this title already exists in the HTML."""
    # Check both escaped and literal forms (BS4 serializes apostrophes as literal ')
    return (
        f'<h4 class="article-title">{_html.escape(title)}</h4>' in html
        or f'<h4 class="article-title">{title}</h4>' in html
    )


def _section_end(html: str, section_start: int) -> int:
    """Return index of the start of the next theme-section (or </div> closing mindmap)."""
    rest = html[section_start:]
    next_sec = re.search(r'<div class="theme-section"', rest[1:])
    if next_sec:
        return section_start + 1 + next_sec.start()
    # End of mindmap container
    end = html.rfind("</div>", section_start)
    return end if end != -1 else len(html)


def _insert_card_into_html(html_path: Path, card: dict) -> bool:
    """Insert a new article-card into the correct theme section, newest-first by date.

    Returns True if inserted, False if already present (idempotent).
    """
    html = html_path.read_text(encoding="utf-8")

    if _card_already_in_html(html, card["title"]):
        return False

    html = _ensure_data_theme_attrs(html)

    theme_kw = card["theme_keyword"]
    card_html = _render_card_html(card)
    new_date = card.get("iso_date", "")

    section_pattern = re.compile(
        rf'<div class="theme-section"[^>]*\bdata-theme="{re.escape(theme_kw)}"'
    )
    m = section_pattern.search(html)

    if m is None:
        html = html.replace("</body>", card_html + "</body>")
    else:
        sec_end = _section_end(html, m.start())
        section_html = html[m.start():sec_end]

        # Find first existing card whose data-date is older than new_date → insert before it
        insert_at = None
        if new_date:
            for cm in re.finditer(r'<div class="article-card"[^>]*data-date="([^"]*)"', section_html):
                if cm.group(1) < new_date:
                    insert_at = m.start() + cm.start()
                    break

        if insert_at is None:
            # No older card found → append at end of section
            insert_at = sec_end
        html = html[:insert_at] + card_html + html[insert_at:]

    shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
    tmp = html_path.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(html_path)
    return True


_EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_en_date(d: datetime) -> str:
    return f"{_EN_MONTHS[d.month - 1]} {d.day}, {d.year}"


_CENTRAL_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A punchy 5–10 word headline capturing the dominant AI narrative right now.",
        },
        "description": {
            "type": "string",
            "description": "2–3 sentences describing what this moment in AI is really about, grounded in specifics from the episodes.",
        },
    },
    "required": ["title", "description"],
}


def _generate_central_node_text(md_dir: Path) -> tuple[str, str] | None:
    """Generate a fresh (title, description) for the central node via LLM.

    Returns None on any failure so the caller can keep existing text.
    """
    summaries_blob = _collect_summaries_blob(md_dir, max_chars=40_000)
    if not summaries_blob:
        return None

    import llm
    messages = [
        {
            "role": "system",
            "content": (
                "You write punchy editorial copy for AI trend pages. "
                "Output ONLY valid JSON matching the requested schema."
            ),
        },
        {
            "role": "user",
            "content": (
                "Based on the following AI Daily Brief episode summaries, write a "
                'central-node "title" (5–10 words) and "description" for an HTML mindmap page. '
                "The title should capture the dominant theme or inflection point of "
                "this particular batch of episodes — not generic AI boosterism. "
                "The description should mention specific developments or tensions from "
                "the episodes (3–4 sentences max).\n\n"
                f"Episode summaries:\n{summaries_blob}"
            ),
        },
    ]
    try:
        result = llm.chat_json(messages, schema=_CENTRAL_NODE_SCHEMA, max_tokens=300, temperature=0.3)
    except Exception as exc:
        print(f"  [central-node] LLM call failed: {exc}")
        return None

    if not isinstance(result, dict):
        return None
    # Accept "headline" as a fallback key in case the model ignores the schema property name
    title = (result.get("title") or result.get("headline") or "").strip()
    description = result.get("description", "").strip()
    if not title or not description:
        print(f"  [central-node] unexpected keys in result: {list(result.keys())}")
        return None
    return title, description


_LATEST_CARD_CSS = """
  /* ── Latest Panels Row (episode + article side-by-side) ──── */
  .latest-panels-row {
    grid-column: 1 / -1;
    display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 4px;
  }
  .latest-episode, .latest-article {
    flex: 1 1 300px; min-width: 0;
    padding: 18px 22px;
    border-radius: 14px;
    box-shadow: 0 6px 24px rgba(0,0,0,0.30);
  }
  .latest-episode {
    background: linear-gradient(135deg, rgba(0,242,254,0.10), rgba(79,172,254,0.08));
    border: 1px solid rgba(0,242,254,0.25);
  }
  .latest-article {
    background: linear-gradient(135deg, rgba(255,200,100,0.10), rgba(255,159,67,0.08));
    border: 1px solid rgba(255,200,100,0.25);
  }
  .latest-episode-header, .latest-article-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; flex-wrap: wrap; margin-bottom: 12px;
  }
  .latest-episode-label {
    font-size: 0.68em; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; color: #00f2fe;
    background: rgba(0,242,254,0.10); border: 1px solid rgba(0,242,254,0.30);
    padding: 3px 10px; border-radius: 10px;
  }
  .latest-article-label {
    font-size: 0.68em; font-weight: 700; letter-spacing: 1.5px;
    text-transform: uppercase; color: #ffc864;
    background: rgba(255,200,100,0.10); border: 1px solid rgba(255,200,100,0.30);
    padding: 3px 10px; border-radius: 10px;
  }
  .latest-episode-stamp, .latest-article-stamp { font-size: 0.68em; color: #666; }
  .latest-episode .article-card, .latest-article .article-card {
    margin: 0;
    border-color: rgba(0,242,254,0.35);
  }
  .latest-article .article-card {
    border-color: rgba(255,200,100,0.35);
  }
"""


def _inject_latest_card_panel(soup, md_dir: Path) -> None:
    """Insert (or replace) a .latest-panels-row with Latest Episode + Latest Article panels.

    Both panels clone the newest matching article-card. Excluded from theme/actor filters
    via JS (selector targets only section-siblings).
    """
    from bs4 import BeautifulSoup as _BS

    # Remove old panels row and any legacy standalone panels
    for old in soup.find_all("div", class_="latest-panels-row"):
        old.decompose()
    for old in soup.find_all("div", class_="latest-episode"):
        old.decompose()
    for old in soup.find_all("div", class_="latest-article"):
        old.decompose()

    all_cards = soup.find_all("div", class_="article-card")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # Latest Episode: newest podcast card (data-source != "raw")
    episode_cards = [c for c in all_cards if c.get("data-date") and c.get("data-source") != "raw"]
    episode_panel_html = ""
    if episode_cards:
        latest_ep = max(episode_cards, key=lambda c: c.get("data-date", ""))
        cloned_ep = copy.copy(latest_ep)
        ep_shell = (
            '<div class="latest-episode">'
            '<div class="latest-episode-header">'
            '<span class="latest-episode-label">⚡ Latest Episode</span>'
            f'<span class="latest-episode-stamp">updated {timestamp}</span>'
            '</div>'
            '</div>'
        )
        ep_tag = _BS(ep_shell, "html.parser").find("div", class_="latest-episode")
        ep_tag.append(cloned_ep)
        episode_panel_html = str(ep_tag)

    # Latest Article: newest raw-ingest card from the last 30 days only
    cutoff = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    raw_cards = [
        c for c in all_cards
        if c.get("data-source") == "raw" and c.get("data-date", "") >= cutoff
    ]
    article_panel_html = ""
    if raw_cards:
        latest_art = max(raw_cards, key=lambda c: c.get("data-date", ""))
        cloned_art = copy.copy(latest_art)
        art_shell = (
            '<div class="latest-article">'
            '<div class="latest-article-header">'
            '<span class="latest-article-label">📄 Latest Article</span>'
            f'<span class="latest-article-stamp">updated {timestamp}</span>'
            '</div>'
            '</div>'
        )
        art_tag = _BS(art_shell, "html.parser").find("div", class_="latest-article")
        art_tag.append(cloned_art)
        article_panel_html = str(art_tag)

    if not episode_panel_html and not article_panel_html:
        return

    row_html = f'<div class="latest-panels-row">{episode_panel_html}{article_panel_html}</div>'
    row_tag = _BS(row_html, "html.parser").find("div", class_="latest-panels-row")

    mindmap = soup.find("div", class_="mindmap")
    if mindmap:
        mindmap.insert(0, row_tag)


def _update_html_stats_and_ui(html_path: Path, md_dir: Path) -> None:
    """Rebuild the HTML mindmap with BeautifulSoup:
    - Backfill data-date/data-actors on all cards
    - Reassign cards to correct theme sections (fixing old bug)
    - Sort cards newest-first within each section
    - Add actor filter row + two-axis JS
    - Update dynamic stats (episode count, date range)
    - Inject latest-episode panel (first child of .mindmap)
    - Auto-generate central node title + description via LLM
    """
    from bs4 import BeautifulSoup, Tag, NavigableString as _NS

    # Load extra (dynamic) themes added by raw_ingest.py
    import json as _json
    _state_path = Path(__file__).parent / "state.json"
    _extra_themes: list[dict] = []
    if _state_path.exists():
        try:
            _extra_themes = _json.loads(_state_path.read_text(encoding="utf-8")).get("extra_themes", [])
        except Exception:
            pass

    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # ── 0. Patch static page-level strings to English ────────────────────────
    html_tag = soup.find("html")
    if html_tag:
        html_tag["lang"] = "en"

    title_tag = soup.find("title")
    if title_tag:
        # Rebuild date range from file name or just use year
        title_tag.string = "AI Daily Brief — Mind Map"

    h1_tag = soup.find("h1")
    if h1_tag:
        h1_tag.string = "AI Daily Brief — Mind Map"

    # Central node: auto-generate via LLM (fallback: keep existing text)
    central = soup.find("div", class_="central-node")
    if central:
        print("  → Generating central node text...", flush=True)
        cn_result = _generate_central_node_text(md_dir)
        if cn_result:
            cn_title, cn_desc = cn_result
            h2 = central.find("h2")
            if h2:
                h2.string = cn_title
            p = central.find("p")
            if p:
                p.string = cn_desc
            print(f"  ✓ Central node updated: '{cn_title[:60]}'")
        else:
            # Replace known placeholder text left by the HTML template
            h2 = central.find("h2") if central else None
            p = central.find("p") if central else None
            if h2 and h2.get_text().strip() in ("Test Title", ""):
                h2.string = "AI Daily Brief — Mind Map"
            if p and p.get_text().strip() in ("Test description.", ""):
                p.string = "Visual summary of recent AI Daily Brief episodes."
            print("  [central-node] LLM unavailable — keeping existing text")

    # Theme section h3 + theme-desc
    _THEME_H3 = {kw: label for kw, label in THEMES}
    for section in soup.find_all("div", class_="theme-section"):
        kw = section.get("data-theme", "")
        if not kw:
            continue
        h3 = section.find("h3")
        if h3:
            h3.string = _THEME_H3.get(kw, h3.get_text())
        desc_p = section.find("p", class_="theme-desc")
        if desc_p:
            desc_p.string = THEME_DESCS.get(kw, desc_p.get_text())

    # Theme filter buttons
    _THEME_LABEL = {kw: label.split(" ", 1)[-1] if " " in label else label for kw, label in THEMES}
    theme_controls = soup.find("div", class_="theme-controls")
    if theme_controls:
        for btn in theme_controls.find_all("button", class_="filter-btn"):
            df = btn.get("data-filter", "")
            if df == "all":
                btn.string = "All"
            elif df in _THEME_LABEL:
                btn.string = _THEME_LABEL[df]
        # Ensure buttons exist for extra (dynamic) themes
        existing_filters = {btn.get("data-filter") for btn in theme_controls.find_all("button")}
        from bs4 import BeautifulSoup as _BS2
        for et in _extra_themes:
            kw_et = et.get("keyword", "")
            if kw_et and kw_et not in existing_filters:
                label_et = et.get("label", kw_et)
                short_et = label_et.split(" ", 1)[-1] if " " in label_et else label_et
                new_btn_html = f'<button class="filter-btn theme-btn" data-filter="{kw_et}">{short_et}</button>'
                new_btn = _BS2(new_btn_html, "html.parser").find("button")
                theme_controls.append(new_btn)

    # ── 1. Build title→meta lookup from md files ─────────────────────────────
    from urllib.parse import quote as _url_quote
    title_meta: dict[str, dict] = {}
    for md_path_item in md_dir.glob("*.md"):
        text = md_path_item.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        if not fm:
            continue
        title = _get_fm_value(fm, "title") or md_path_item.stem
        pub_date = _get_fm_value(fm, "published")
        iso = _pub_date_to_iso(pub_date)
        summary_match = re.search(r'^## Summary\n+(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
        summary_text = summary_match.group(1).strip() if summary_match else ""
        actors = _detect_actors(f"{title} {summary_text}")
        note_path = f"projects/SpotifyTranscript/Transcriptions/{md_path_item.name}"
        obsidian_uri = f"obsidian://open?vault=Nelson&file={_url_quote(note_path)}"
        title_meta[title] = {
            "iso": iso,
            "actors": actors,
            "summary_full": summary_text,
            "obsidian_uri": obsidian_uri,
        }

    # ── 2. Backfill all data-* attrs on every card ───────────────────────────
    for card in soup.find_all("div", class_="article-card"):
        h4 = card.find("h4", class_="article-title")
        if not h4:
            continue
        card_title = h4.get_text()
        meta = title_meta.get(card_title)
        if meta:
            card["data-date"] = meta["iso"]
            card["data-actors"] = ",".join(meta["actors"])
            card["data-summary"] = meta["summary_full"]
            card["data-obsidian"] = meta["obsidian_uri"]
        else:
            if not card.get("data-date"):
                card["data-date"] = ""
            if not card.get("data-actors"):
                card["data-actors"] = ""

    # ── 3. Deduplicate: one card per title, prefer LLM-generated (has data-date) ─
    # Drop undated cards UNLESS they are raw-ingest cards (data-source="raw") —
    # raw notes may legitimately lack a publication date and must not be lost.
    seen: dict[str, Tag] = {}
    for card in soup.find_all("div", class_="article-card"):
        h4 = card.find("h4", class_="article-title")
        if not h4:
            continue
        if not card.get("data-date") and card.get("data-source") != "raw":
            continue  # drop undated legacy podcast cards
        title = h4.get_text().strip()
        if title not in seen:
            seen[title] = card

    # Remove ALL cards from DOM; we'll re-insert only the deduplicated set
    for card in soup.find_all("div", class_="article-card"):
        card.extract()

    # ── 4. Group deduplicated cards by theme ─────────────────────────────────
    from collections import defaultdict
    cards_by_theme: dict[str, list[Tag]] = defaultdict(list)
    for card in seen.values():
        theme = card.get("data-tags", "")
        cards_by_theme[theme].append(card)

    # ── 5. Ensure each theme-section has correct data-theme + accent color ─────
    for kw, label in THEMES:
        label_keyword = label.split(" ", 1)[-1].split("&")[0].strip() if " " in label else label
        accent = _ACCENT_COLORS.get(kw, "#4facfe")
        for section in soup.find_all("div", class_="theme-section"):
            h3 = section.find("h3")
            if h3 and label_keyword in h3.get_text():
                for attr in list(section.attrs.keys()):
                    if attr == "data-theme":
                        del section[attr]
                section["data-theme"] = kw
                section["style"] = f"--accent:{accent}"
                break
    # Same pass for dynamic extra themes (raw_ingest.py may have added new sections)
    for et in _extra_themes:
        kw_et = et.get("keyword", "")
        label_et = et.get("label", "")
        accent_et = et.get("accent", "#4facfe")
        if not kw_et:
            continue
        for section in soup.find_all("div", class_="theme-section"):
            if section.get("data-theme") == kw_et:
                section["style"] = f"--accent:{accent_et}"
                break

    # Patch CSS: make .theme-section h3 use --accent instead of hardcoded blue
    style_tag = soup.find("style")
    if style_tag and style_tag.string:
        style_tag.string = re.sub(
            r'(\.theme-section h3 \{[^}]*?)background: linear-gradient\(90deg, #00f2fe, #4facfe\);',
            r'\1background: linear-gradient(90deg, var(--accent, #00f2fe), color-mix(in srgb, var(--accent, #4facfe) 60%, white));',
            style_tag.string,
        )

    # ── 6. Re-insert cards into the correct sections, sorted newest-first ─────
    def _card_sort_key(card: Tag) -> str:
        return card.get("data-date", "") or ""

    for section in soup.find_all("div", class_="theme-section"):
        kw = section.get("data-theme", "")
        cards = sorted(cards_by_theme.get(kw, []), key=_card_sort_key, reverse=True)
        # Insert all cards immediately after this section element
        insert_after = section
        for card in cards:
            insert_after.insert_after(card)
            insert_after = card

    # ── 7. Update stats ───────────────────────────────────────────────────────
    all_cards = soup.find_all("div", class_="article-card")
    episode_count = len(all_cards)
    dates = sorted([c.get("data-date", "") for c in all_cards if c.get("data-date")])
    days_covered = 0
    date_range_str = ""
    if dates:
        d0 = datetime.strptime(dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
        days_covered = (d1 - d0).days + 1
        date_range_str = f"{_fmt_en_date(d0)} to {_fmt_en_date(d1)}"

    subtitle = soup.find("p", class_="subtitle")
    if subtitle:
        subtitle.string = f"Visual summary of {episode_count} episodes · {date_range_str}"

    _STAT_LABEL_MAP = {
        "Episódios": "Episodes", "Episodes": "Episodes",
        "Dias Cobertos": "Days Covered", "Days Covered": "Days Covered",
        "Grandes Temas": "Main Themes", "Main Themes": "Main Themes",
        "Atores Centrais": "Central Actors", "Central Actors": "Central Actors",
    }
    for stat in soup.find_all("div", class_="stat"):
        label_div = stat.find("div", class_="stat-label")
        value_div = stat.find("div", class_="stat-value")
        if not label_div or not value_div:
            continue
        label_text = label_div.get_text().strip()
        en_label = _STAT_LABEL_MAP.get(label_text)
        if en_label:
            label_div.string = en_label
        if "Episodes" in (en_label or label_text) or "Episódios" in label_text:
            value_div.string = str(episode_count)
        elif "Days" in (en_label or label_text) or "Dias" in label_text:
            value_div.string = str(days_covered)

    # ── 7b. Update footer ────────────────────────────────────────────────────
    footer = soup.find("footer")
    if footer and dates:
        def _short_en(d: datetime) -> str:
            return f"{_EN_MONTHS[d.month - 1]} {d.day}"
        range_short = f"{_short_en(d0)} — {_short_en(d1)}, {d1.year}"
        footer.clear()
        footer.append(_NS(
            f"Mind map generated from AI Daily Brief (NLW) episode summaries"
            f" · {range_short}"
        ))
        footer.append(soup.new_tag("br"))
        total_themes = len(THEMES) + len(_extra_themes)
        footer.append(_NS(
            f"Click the filters above to navigate by theme · {episode_count} episodes · {total_themes} main themes"
        ))

    # ── 8. Add/update actor filter row ───────────────────────────────────────
    controls_divs = soup.find_all("div", class_="controls")
    actor_controls = soup.find("div", class_="actor-controls")

    # Build the actor controls div
    from bs4 import BeautifulSoup as BS
    actor_html = (
        '<div class="controls actor-controls">'
        '<button class="filter-btn actor-btn active" data-actor="all">All Actors</button>'
        + "".join(
            f'<button class="filter-btn actor-btn" data-actor="{_html.escape(a)}">{_html.escape(a)}</button>'
            for a in ACTORS
        )
        + "</div>"
    )
    new_actor_div = BS(actor_html, "html.parser").find("div")

    theme_controls = soup.find("div", class_="theme-controls")
    if not theme_controls:
        # Find the existing controls div and add theme-controls class
        plain_controls = soup.find("div", class_="controls")
        if plain_controls:
            plain_controls["class"] = ["controls", "theme-controls"]
            theme_controls = plain_controls

    if actor_controls:
        actor_controls.replace_with(new_actor_div)
    elif theme_controls:
        theme_controls.insert_before(new_actor_div)

    # Add theme-btn class to theme buttons; translate "All" button
    if theme_controls:
        for btn in theme_controls.find_all("button", class_="filter-btn"):
            classes = btn.get("class", [])
            if "theme-btn" not in classes:
                btn["class"] = classes + ["theme-btn"]
            if btn.get("data-filter") == "all" and btn.get_text().strip() in ("Todos", "All"):
                btn.string = "All"

    # ── 9. Replace script with two-axis filter ────────────────────────────────
    script_tag = soup.find("script")
    new_js = """
document.addEventListener('DOMContentLoaded', function() {
  let activeTheme = 'all';
  let activeActor = 'all';

  function applyFilters() {
    // Only filter cards that live inside a theme section (skip .latest-episode panel)
    const cards = document.querySelectorAll('.theme-section ~ .article-card');
    const sections = document.querySelectorAll('.theme-section');

    cards.forEach(card => {
      const themeOk = activeTheme === 'all' || card.dataset.tags === activeTheme;
      const actorOk = activeActor === 'all' || (card.dataset.actors || '').split(',').includes(activeActor);
      card.classList.toggle('hidden', !(themeOk && actorOk));
    });

    sections.forEach(section => {
      const sectionTheme = section.dataset.theme;
      if (activeTheme !== 'all' && sectionTheme !== activeTheme) {
        section.classList.add('hidden');
        return;
      }
      let next = section.nextElementSibling;
      let anyVisible = false;
      while (next && !next.classList.contains('theme-section')) {
        if (next.classList.contains('article-card') && !next.classList.contains('hidden')) {
          anyVisible = true;
        }
        next = next.nextElementSibling;
      }
      section.classList.toggle('hidden', !anyVisible);
    });
  }

  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.theme-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeTheme = btn.dataset.filter;
      applyFilters();
    });
  });

  document.querySelectorAll('.actor-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.actor-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeActor = btn.dataset.actor;
      applyFilters();
    });
  });

  // ── Modal ──────────────────────────────────────────────────────────────────
  const modal = document.getElementById('episode-modal');
  const modalTitle = document.getElementById('modal-title');
  const modalDate = document.getElementById('modal-date');
  const modalSummary = document.getElementById('modal-summary');
  const modalObsidian = document.getElementById('modal-obsidian');

  document.querySelectorAll('.article-card').forEach(card => {
    card.addEventListener('click', (e) => {
      if (e.target.closest('a')) return; // don't intercept link clicks
      const title = card.querySelector('.article-title')?.textContent || '';
      const date = card.querySelector('.article-date')?.textContent || '';
      const summaryRaw = card.dataset.summary || '';
      const obsidianUri = card.dataset.obsidian || '';

      modalTitle.textContent = title;
      modalDate.textContent = date;

      // Render numbered bullet list
      const lines = summaryRaw.split('\\n').filter(l => l.trim());
      modalSummary.innerHTML = lines.map(l => `<li>${l.replace(/^\\d+\\.\\s*/, '')}</li>`).join('');

      if (modalObsidian) {
        if (obsidianUri) {
          modalObsidian.href = obsidianUri;
          modalObsidian.style.display = 'inline-flex';
        } else {
          modalObsidian.style.display = 'none';
        }
      }

      modal.classList.remove('hidden');
      modal.setAttribute('aria-hidden', 'false');
    });
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

  function closeModal() {
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
  }
}); // end DOMContentLoaded
"""
    if script_tag:
        script_tag.string = new_js

    # ── 9b. Inject modal HTML + CSS (idempotent) ─────────────────────────────
    if not soup.find(id="episode-modal"):
        modal_html = """
<div id="episode-modal" class="hidden" role="dialog" aria-modal="true" aria-hidden="true">
  <div class="modal-backdrop"></div>
  <div class="modal-box">
    <button id="modal-close" aria-label="Close">&times;</button>
    <div class="modal-header">
      <p id="modal-date"></p>
      <h2 id="modal-title"></h2>
    </div>
    <ol id="modal-summary"></ol>
    <a id="modal-obsidian" href="#">
      <svg width="16" height="16" viewBox="0 0 100 100" fill="currentColor" aria-hidden="true">
        <path d="M50 5C25.1 5 5 25.1 5 50s20.1 45 45 45 45-20.1 45-45S74.9 5 50 5zm0 80c-19.3 0-35-15.7-35-35S30.7 15 50 15s35 15.7 35 35-15.7 35-35 35z"/>
        <path d="M50 30c-11 0-20 9-20 20s9 20 20 20 20-9 20-20-9-20-20-20z"/>
      </svg>
      Open in Obsidian
    </a>
  </div>
</div>"""
        from bs4 import BeautifulSoup as _BS
        modal_tag = _BS(modal_html, "html.parser").find("div", id="episode-modal")
        body_tag = soup.find("body")
        if body_tag:
            body_tag.append(modal_tag)
    else:
        # Patch existing modal strings to English (idempotent)
        close_btn = soup.find(id="modal-close")
        if close_btn:
            close_btn["aria-label"] = "Close"
        obsidian_link = soup.find(id="modal-obsidian")
        if obsidian_link:
            obsidian_link.attrs.pop("target", None)
            obsidian_link.attrs.pop("rel", None)
            for child in list(obsidian_link.children):
                if isinstance(child, _NS) and child.strip():
                    child.replace_with(_NS("\n      Open in Obsidian\n    "))

    modal_css = """
  /* ── Modal ───────────────────────────────────────────────── */
  #episode-modal { position:fixed; inset:0; z-index:1000; display:flex; align-items:center; justify-content:center; }
  #episode-modal.hidden { display:none; }
  .modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,0.75); backdrop-filter:blur(4px); }
  .modal-box {
    position:relative; z-index:1; background:linear-gradient(135deg,#1a1040,#2a1f5c);
    border:1px solid rgba(255,255,255,0.15); border-radius:16px;
    padding:32px; max-width:680px; width:90%; max-height:80vh;
    overflow-y:auto; box-shadow:0 20px 60px rgba(0,0,0,0.6);
  }
  #modal-close {
    position:absolute; top:16px; right:16px; background:rgba(255,255,255,0.1);
    border:none; color:#e8e8e8; font-size:1.4em; width:32px; height:32px;
    border-radius:50%; cursor:pointer; line-height:1; display:flex; align-items:center; justify-content:center;
  }
  #modal-close:hover { background:rgba(255,255,255,0.2); }
  .modal-header { margin-bottom:20px; }
  #modal-date { color:#aaa; font-size:0.8em; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }
  #modal-title { font-size:1.4em; color:#fff; line-height:1.3; }
  #modal-summary { padding-left:20px; margin:0 0 24px; }
  #modal-summary li { color:#c8c8c8; font-size:0.9em; line-height:1.6; margin-bottom:6px; }
  #modal-obsidian {
    display:inline-flex; align-items:center; gap:8px;
    background:linear-gradient(90deg,#7c3aed,#a855f7); color:#fff;
    padding:10px 20px; border-radius:20px; text-decoration:none;
    font-size:0.85em; font-weight:600; transition:opacity 0.2s;
  }
  #modal-obsidian:hover { opacity:0.85; }
"""
    style_tag = soup.find("style")
    if style_tag and style_tag.string and "episode-modal" not in style_tag.string:
        style_tag.string = style_tag.string + modal_css

    # ── 9c. Latest panels row + Personal takeaways (regenerated each sync) ──────
    style_tag = soup.find("style")
    if style_tag and style_tag.string and "latest-panels-row" not in style_tag.string:
        # Remove old latest-episode CSS block if present to avoid duplication
        style_tag.string = re.sub(
            r'/\* ── Latest Episode Panel.*?(?=\n  /\*|\Z)', '',
            style_tag.string, flags=re.DOTALL
        )
        style_tag.string = style_tag.string + _LATEST_CARD_CSS
    print("  → Injecting latest panels row...", flush=True)
    _inject_latest_card_panel(soup, md_dir)
    print("  ✓ Latest panels row injected")

    _ensure_personal_takeaways_css(soup)
    print("  → Generating personal takeaways...", flush=True)
    takeaways = _generate_personal_takeaways(md_dir)
    if takeaways:
        _inject_personal_takeaways_section(soup, takeaways)
        print(f"  ✓ Personal takeaways section updated ({len(takeaways)} items)")

    # ── 10. Write ─────────────────────────────────────────────────────────────
    out = str(soup)
    shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
    tmp = html_path.with_suffix(".html.tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(html_path)
    print(f"  ✓ HTML stats/UI updated ({episode_count} cards, {days_covered} days)")


# ── Web variant (no Obsidian links) ─────────────────────────────────────────

def _generate_web_variant(html_path: Path) -> Path:
    """Produce a web-safe copy of the mindmap with all Obsidian-specific bits removed.

    Strips:
    - #modal-obsidian element (button that opens obsidian:// URIs)
    - data-obsidian attributes on every card

    Returns the path of the written web variant file.
    """
    from bs4 import BeautifulSoup

    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    obsidian_btn = soup.find(id="modal-obsidian")
    if obsidian_btn:
        obsidian_btn.decompose()

    for card in soup.find_all("div", class_="article-card"):
        if "data-obsidian" in card.attrs:
            del card["data-obsidian"]

    web_path = html_path.parent / "AI Daily Brief - Mind Map (Web).html"
    web_path.write_text(str(soup), encoding="utf-8")
    return web_path


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

    title = _get_fm_value(fm_block, "title") or md_path.stem
    show = _get_fm_value(fm_block, "show")
    pub_date = _get_fm_value(fm_block, "published")
    is_ai_brief = show.startswith(SHOW_AI_DAILY_BRIEF)

    summarized = _is_summarized(fm_block)

    # For AI Daily Brief: also check if the HTML card is already present so we
    # can retry the HTML step independently if it failed on a previous run.
    needs_html = (
        is_ai_brief
        and html_path is not None
        and html_path.exists()
        and not _card_already_in_html(html_path.read_text(encoding="utf-8"), title)
    )

    if summarized and not needs_html:
        print(f"  [skip] Already done: {md_path.name}")
        return

    print(f"\n  → {md_path.name}")
    print(f"    Title : {title}")
    print(f"    Show  : {show}")

    if not summarized:
        # Extract transcript section
        transcript_match = re.search(r'^## Transcript\n+(.*)', body, re.DOTALL | re.MULTILINE)
        transcript = transcript_match.group(1).strip() if transcript_match else body.strip()
        transcript = _clip_transcript(transcript)

        # Step 1: Generate 20-point summary
        print("    Generating summary...", flush=True)
        messages = _build_summary_messages(title, transcript)
        summary = llm.chat(messages, max_tokens=1024, temperature=0.1)
        print(f"    Summary: {len(summary)} chars")
    else:
        # Summary already written — extract it from the body for the HTML card step
        summary_match = re.search(r'^## Summary\n+(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
        summary = summary_match.group(1).strip() if summary_match else ""
        print("    Summary already present, reusing for HTML card.")

    # Step 2: HTML card for AI Daily Brief (before marking summarized)
    if needs_html:
        print("    Generating HTML card...", flush=True)
        card_messages = _build_card_messages(title, pub_date, summary)
        card = llm.chat_json(card_messages, schema=_CARD_SCHEMA, max_tokens=512, temperature=0.1)
        card.setdefault("title", title)  # model occasionally omits this field
        card["actors"] = _detect_actors(f"{title} {card.get('summary_en', card.get('summary_pt', ''))} {' '.join(card.get('tags', []))}")
        card["iso_date"] = _pub_date_to_iso(pub_date)
        card["summary_full"] = summary
        card["obsidian_uri"] = _obsidian_uri(md_path.name)
        inserted = _insert_card_into_html(html_path, card)
        if inserted:
            print(f"    ✓ Card inserted into HTML (theme: {card['theme_keyword']})")
        else:
            print(f"    [skip] Card already in HTML")

    # Step 3: Persist summary + mark summarized (only if not already done)
    if not summarized:
        new_body = _insert_summary(body, summary)
        new_fm = _set_fm_flag(fm_block, "summarized", "true")
        tmp_md = md_path.with_suffix(".md.tmp")
        tmp_md.write_text(new_fm + new_body, encoding="utf-8")
        tmp_md.replace(md_path)
        print(f"    ✓ Summary written to {md_path.name}")


# ── English card regen ────────────────────────────────────────────────────────

def _regen_english_cards(md_files: list[Path], html_path: Path) -> None:
    """Re-generate card visible text (date, summary, key-points, tags) in English
    for every AI Daily Brief md that lacks `card_english: true`.

    Updates the card in the HTML DOM directly (BS4), then sets the flag so the
    step is not repeated on future runs.
    """
    import llm
    from bs4 import BeautifulSoup

    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    changed = False

    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        fm_block, body = _split_frontmatter(text)
        if not fm_block:
            continue
        show = _get_fm_value(fm_block, "show")
        if not show.startswith(SHOW_AI_DAILY_BRIEF):
            continue
        if _is_card_english(fm_block):
            continue

        title = _get_fm_value(fm_block, "title") or md_path.stem
        pub_date = _get_fm_value(fm_block, "published")

        if not _is_summarized(fm_block):
            print(f"  [skip regen] Not yet summarized: {md_path.name}")
            continue

        summary_match = re.search(r'^## Summary\n+(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
        summary = summary_match.group(1).strip() if summary_match else ""

        print(f"  → Regen English card: {md_path.name}")
        try:
            card_messages = _build_card_messages(title, pub_date, summary)
            card = llm.chat_json(card_messages, schema=_CARD_SCHEMA, max_tokens=512, temperature=0.1)
            card.setdefault("title", title)
        except Exception as exc:
            print(f"    ✗ LLM failed: {exc}")
            continue

        date_en = card.get("date_en", "")
        summary_en = card.get("summary_en", "")
        key_points = card.get("key_points", [])
        tags = card.get("tags", [])

        # Find this card in the DOM by title
        card_tag = None
        for c in soup.find_all("div", class_="article-card"):
            h4 = c.find("h4", class_="article-title")
            if h4 and h4.get_text().strip() == title:
                card_tag = c
                break

        if card_tag is None:
            print(f"    [warn] Card not found in HTML, skipping DOM patch")
        else:
            date_div = card_tag.find("div", class_="article-date")
            if date_div:
                date_div.string = date_en
            summary_p = card_tag.find("p", class_="article-summary")
            if summary_p:
                summary_p.string = summary_en
            ul = card_tag.find("ul", class_="key-points")
            if ul:
                ul.clear()
                for pt in key_points:
                    li = soup.new_tag("li")
                    li.string = pt
                    ul.append(li)
            tags_div = card_tag.find("div", class_="tags")
            if tags_div:
                tags_div.clear()
                for t in tags:
                    span = soup.new_tag("span", attrs={"class": "tag"})
                    span.string = t
                    tags_div.append(span)
            changed = True
            print(f"    ✓ Card updated in HTML")

        # Mark flag in frontmatter
        new_fm = _set_fm_flag(fm_block, "card_english", "true")
        tmp_md = md_path.with_suffix(".md.tmp")
        tmp_md.write_text(new_fm + body, encoding="utf-8")
        tmp_md.replace(md_path)

    if changed:
        out = str(soup)
        shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
        tmp = html_path.with_suffix(".html.tmp")
        tmp.write_text(out, encoding="utf-8")
        tmp.replace(html_path)
        print("  ✓ HTML saved after English card regen")


# ── Main ─────────────────────────────────────────────────────────────────────

def _strip_resumo(md_files: list[Path]) -> None:
    """Remove legacy ## **RESUMO** sections from md files (idempotent)."""
    pattern = re.compile(r'\n## \*\*RESUMO\*\*.*', re.DOTALL)
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        cleaned = pattern.sub("", text)
        if cleaned != text:
            md_path.write_text(cleaned, encoding="utf-8")
            print(f"  ✓ Removed RESUMO section: {md_path.name}")


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

    # Strip legacy ## **RESUMO** sections (one-time migration, idempotent)
    _strip_resumo(md_files)

    processed = 0
    failed = 0
    for md_path in md_files:
        try:
            process_file(md_path, html_path if html_path.exists() else None)
            processed += 1
        except Exception as exc:
            print(f"  ✗ Failed {md_path.name}: {exc}")
            failed += 1

    # Regen card text to English for any card that hasn't been translated yet
    if html_path.exists():
        print("\nRegenerating English card content...")
        try:
            _regen_english_cards(md_files, html_path)
        except Exception as exc:
            print(f"  ✗ English card regen failed: {exc}")

    # Always run HTML post-pass: backfill attrs, sort, update stats/UI
    if html_path.exists():
        print("\nUpdating HTML mindmap (stats, filters, sort)...")
        try:
            _update_html_stats_and_ui(html_path, transcriptions_dir)
        except Exception as exc:
            print(f"  ✗ HTML update failed: {exc}")

        print("\nGenerating web variant (no Obsidian links)...")
        try:
            web_path = _generate_web_variant(html_path)
            print(f"  ✓ Web variant written: {web_path.name}")
        except Exception as exc:
            print(f"  ✗ Web variant failed: {exc}")

    print(f"\n{'='*40}")
    print(f"Done. {processed} file(s) processed, {failed} failed.")
    print(f"Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")


if __name__ == "__main__":
    main()
