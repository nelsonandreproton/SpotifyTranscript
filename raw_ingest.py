#!/usr/bin/env python3
"""
raw_ingest.py — Ingest raw Obsidian notes into the AI Daily Brief HTML mindmap.

For each .md in OBSIDIAN_RAW_PATH that lacks `ingested: true` in frontmatter:
  1. Skip if body content < 200 chars (marks ingested:true anyway to avoid retry).
  2. Generate a 20-point summary via LLM.
  3. Write knowledge/sources/{slug}.md in the Obsidian vault.
  4. Build an HTML card and insert it into the canonical mindmap HTML.
  5. Handle dynamic new themes: write theme-section + filter button + add to state.json.
  6. Mark `ingested: true` in the raw note's frontmatter.

Called by run_daily.bat BEFORE post_process.py.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import sys
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import quote as _url_quote

from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

MIN_BODY_CHARS = 200

_STATIC_THEMES: list[tuple[str, str]] = [
    ("modelos", "🤖 Models & Harnesses"),
    ("negocio", "💰 Business Model"),
    ("trabalho", "👥 Work & Jobs"),
    ("infraestrutura", "🏗️ Infrastructure"),
    ("sociedade", "🌐 Society & Policy"),
]

_STATIC_THEME_DESCS: dict[str, str] = {
    "modelos": "The competition has shifted: it's now more about the environment around the model than the model itself.",
    "negocio": "How AI is reshaping business models, pricing, and competitive dynamics across the industry.",
    "trabalho": "The ways AI is changing how we work, hire, and think about productivity and career paths.",
    "infraestrutura": "Compute, energy, data-center build-out, and the infrastructure race underpinning the AI boom.",
    "sociedade": "Policy, safety, public perception, and the broader societal implications of rapid AI deployment.",
}

# Accent colors for dynamic themes (cycle through these)
_DYNAMIC_ACCENT_PALETTE = [
    "#ff9f43", "#ee5a24", "#9b59b6", "#1abc9c", "#3498db",
    "#e74c3c", "#f39c12", "#2ecc71", "#e67e22", "#16a085",
]

_STATIC_ACCENT_COLORS: dict[str, str] = {
    "modelos": "#00f2fe",
    "negocio": "#ffd700",
    "trabalho": "#ff7eb3",
    "infraestrutura": "#43e97b",
    "sociedade": "#fa709a",
}

# ── Frontmatter helpers (mirrors post_process.py) ────────────────────────────

_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str, str]:
    m = _FM_PATTERN.match(text)
    if not m:
        return ("", text)
    return (m.group(0), text[m.end():])


def _get_fm_value(frontmatter: str, key: str) -> str:
    pattern = re.compile(rf'^{re.escape(key)}:\s*"?(.*?)"?\s*$', re.MULTILINE)
    m = pattern.search(frontmatter)
    return m.group(1).strip('"') if m else ""


def _set_fm_flag(frontmatter: str, key: str, value: str) -> str:
    pattern = re.compile(rf'^{re.escape(key)}:.*$', re.MULTILINE)
    if pattern.search(frontmatter):
        return pattern.sub(f'{key}: {value}', frontmatter)
    inner = re.sub(r'\n---\n$', f'\n{key}: {value}\n---\n', frontmatter)
    return inner


def _is_ingested(frontmatter: str) -> bool:
    return _get_fm_value(frontmatter, "ingested").lower() == "true"


def _mark_ingested(md_path: Path, frontmatter: str, body: str) -> None:
    new_fm = _set_fm_flag(frontmatter, "ingested", "true")
    tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text(new_fm + body, encoding="utf-8")
    tmp.replace(md_path)


# ── State (dynamic themes) ───────────────────────────────────────────────────

def _load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_extra_themes(state: dict) -> list[dict]:
    """Return list of {keyword, label, desc, accent} for non-static themes."""
    return state.get("extra_themes", [])


def _all_themes(state: dict) -> list[tuple[str, str]]:
    """Static themes + dynamic extras as (keyword, label) pairs."""
    result = list(_STATIC_THEMES)
    for t in _get_extra_themes(state):
        result.append((t["keyword"], t["label"]))
    return result


def _accent_for(keyword: str, state: dict) -> str:
    if keyword in _STATIC_ACCENT_COLORS:
        return _STATIC_ACCENT_COLORS[keyword]
    for t in _get_extra_themes(state):
        if t["keyword"] == keyword:
            return t["accent"]
    return "#aaaaaa"


def _add_extra_theme(state: dict, keyword: str, label: str, desc: str, accent: str) -> None:
    extras: list[dict] = state.setdefault("extra_themes", [])
    for t in extras:
        if t["keyword"] == keyword:
            return  # already exists
    extras.append({"keyword": keyword, "label": label, "desc": desc, "accent": accent})


def _pick_dynamic_accent(state: dict) -> str:
    used = {t["accent"] for t in _get_extra_themes(state)}
    used |= set(_STATIC_ACCENT_COLORS.values())
    for c in _DYNAMIC_ACCENT_PALETTE:
        if c not in used:
            return c
    return _DYNAMIC_ACCENT_PALETTE[len(_get_extra_themes(state)) % len(_DYNAMIC_ACCENT_PALETTE)]


# ── LLM schema / prompts ─────────────────────────────────────────────────────

def _build_raw_card_schema(all_theme_keywords: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "theme_keyword": {
                "type": "string",
                "enum": all_theme_keywords,
                "description": "Best-matching theme keyword from the list, or propose a new short slug.",
            },
            "new_theme_label": {
                "type": "string",
                "description": "If theme_keyword is a NEW slug not in the list, provide a display label (emoji + name). Leave empty otherwise.",
            },
            "new_theme_desc": {
                "type": "string",
                "description": "If theme_keyword is new, provide a 1-sentence description. Leave empty otherwise.",
            },
            "date_en": {
                "type": "string",
                "description": "Publication date in English format, e.g. 'Apr 7, 2026'.",
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


def _build_summary_messages(title: str, content: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a precise content summarizer. "
                "Follow the user's format exactly. Be factual and concise."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Summarize the following article/note titled \"{title}\" "
                f"as exactly 20 numbered bullet points. "
                f"Each point must be one concise sentence capturing a distinct key insight. "
                f"Use the format:\n1. ...\n2. ...\n...\n20. ...\n\n"
                f"CONTENT:\n{content}"
            ),
        },
    ]


def _build_card_messages(
    title: str,
    pub_date: str,
    summary_bullets: str,
    all_themes: list[tuple[str, str]],
) -> list[dict]:
    theme_desc = "\n".join(f"  - {kw}: {label}" for kw, label in all_themes)
    kw_list = ", ".join(kw for kw, _ in all_themes)
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
                f"Given this article summary, produce a JSON card for an HTML mindmap.\n\n"
                f"Article title: {title}\n"
                f"Date: {pub_date}\n\n"
                f"Summary:\n{summary_bullets}\n\n"
                f"Available themes ({kw_list}):\n{theme_desc}\n\n"
                f"Rules:\n"
                f"- theme_keyword: pick the best existing keyword OR propose a short new slug "
                f"(lowercase, no spaces). If new, fill new_theme_label and new_theme_desc.\n"
                f"- date_en: convert '{pub_date}' to English format, e.g. 'Apr 7, 2026'\n"
                f"- summary_en: 1–2 sentences in English\n"
                f"- key_points: 2–4 items in English, each under 80 chars\n"
                f"- tags: 1–4 short English topic tags\n"
            ),
        },
    ]


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _card_already_in_html(html: str, title: str) -> bool:
    return (
        f'<h4 class="article-title">{_html.escape(title)}</h4>' in html
        or f'<h4 class="article-title">{title}</h4>' in html
    )


def _render_raw_card_html(card: dict, state: dict) -> str:
    """Render card HTML with data-source="raw" badge."""
    accent = _accent_for(card["theme_keyword"], state)
    theme_kw = _html.escape(card["theme_keyword"], quote=True)
    iso_date = card.get("iso_date", "")
    summary_full = _html.escape(card.get("summary_full", ""), quote=True)
    source_badge = _html.escape(card.get("source_badge", "Raw Note"))
    tags_html = "".join(
        f'<span class="tag">{_html.escape(t)}</span>' for t in card["tags"]
    )
    points_html = "".join(
        f"      <li>{_html.escape(p)}</li>\n" for p in card["key_points"]
    )
    return (
        f'\n  <div class="article-card" data-tags="{theme_kw}"'
        f' data-actors="" data-date="{iso_date}"'
        f' data-summary="{summary_full}" data-obsidian=""'
        f' data-source="raw"'
        f' style="--accent:{accent}">\n'
        f'    <div class="article-date">{_html.escape(card.get("date_en", ""))}'
        f' <span class="source-badge">{source_badge}</span></div>\n'
        f'    <h4 class="article-title">{_html.escape(card["title"])}</h4>\n'
        f'    <p class="article-summary">{_html.escape(card.get("summary_en", ""))}</p>\n'
        f'    <ul class="key-points">\n'
        f'{points_html}'
        f'    </ul>\n'
        f'    <div class="tags">{tags_html}</div>\n'
        f'  </div>\n'
    )


def _section_end(html: str, section_start: int) -> int:
    """Return the index immediately after the closing </div> of the theme-section.

    Cards should be inserted at this position so they appear after the section header
    and before the next section.
    """
    rest = html[section_start:]
    # Find the next theme-section start (search from char 1 to skip current section tag)
    next_sec = re.search(r'<div class="theme-section"', rest[1:])
    if next_sec:
        return section_start + 1 + next_sec.start()
    # No next section: find the closing </div> of this section div
    close = rest.find("</div>")
    if close != -1:
        return section_start + close + len("</div>")
    return section_start + len(rest)


def _ensure_theme_section_in_html(html_path: Path, keyword: str, label: str, desc: str, accent: str) -> None:
    """Insert a new theme-section div + filter button if not already present."""
    from bs4 import BeautifulSoup, NavigableString as _NS

    html_text = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_text, "html.parser")

    # Check if section already exists
    existing = soup.find("div", attrs={"class": "theme-section", "data-theme": keyword})
    if existing:
        return

    # Build new theme-section
    section_html = (
        f'<div class="theme-section" data-theme="{keyword}" style="--accent:{accent}">'
        f'<h3>{_html.escape(label)}</h3>'
        f'<p class="theme-desc">{_html.escape(desc)}</p>'
        f'</div>'
    )
    from bs4 import BeautifulSoup as _BS
    new_section = _BS(section_html, "html.parser").find("div", class_="theme-section")

    # Insert before closing </div> of .mindmap
    mindmap = soup.find("div", class_="mindmap")
    if mindmap:
        mindmap.append(new_section)
    else:
        body = soup.find("body")
        if body:
            body.append(new_section)

    # Add filter button to theme-controls
    theme_controls = soup.find("div", class_="theme-controls")
    if theme_controls:
        btn_html = f'<button class="filter-btn theme-btn" data-filter="{keyword}">{_html.escape(label.split(" ", 1)[-1] if " " in label else label)}</button>'
        new_btn = _BS(btn_html, "html.parser").find("button")
        theme_controls.append(new_btn)

    # Inject source-badge CSS if not present
    style_tag = soup.find("style")
    if style_tag and style_tag.string and "source-badge" not in style_tag.string:
        style_tag.string = style_tag.string + _SOURCE_BADGE_CSS

    out = str(soup)
    shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
    tmp = html_path.with_suffix(".html.tmp")
    tmp.write_text(out, encoding="utf-8")
    tmp.replace(html_path)
    print(f"    ✓ Theme section added: {keyword} ({label})")


def _insert_raw_card_into_html(html_path: Path, card: dict, state: dict) -> bool:
    """Insert a raw card into the correct theme section. Returns True if inserted."""
    html = html_path.read_text(encoding="utf-8")

    if _card_already_in_html(html, card["title"]):
        return False

    theme_kw = card["theme_keyword"]
    card_html = _render_raw_card_html(card, state)
    new_date = card.get("iso_date", "")

    section_pattern = re.compile(
        rf'<div class="theme-section"[^>]*\bdata-theme="{re.escape(theme_kw)}"'
    )
    m = section_pattern.search(html)

    if m is None:
        # No section found — fall back to appending before </body>
        html = html.replace("</body>", card_html + "</body>")
    else:
        sec_end = _section_end(html, m.start())
        section_html = html[m.start():sec_end]

        insert_at = None
        if new_date:
            for cm in re.finditer(r'<div class="article-card"[^>]*data-date="([^"]*)"', section_html):
                if cm.group(1) < new_date:
                    insert_at = m.start() + cm.start()
                    break

        if insert_at is None:
            insert_at = sec_end
        html = html[:insert_at] + card_html + html[insert_at:]

    # Ensure source-badge CSS is present (idempotent)
    if "source-badge" not in html:
        html = html.replace("</style>", _SOURCE_BADGE_CSS + "\n</style>", 1)

    shutil.copy2(html_path, html_path.with_suffix(".html.bak"))
    tmp = html_path.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(html_path)
    return True


_SOURCE_BADGE_CSS = """
  /* ── Source badge (raw notes) ───────────────────── */
  .source-badge {
    display: inline-block; font-size: 0.62em; font-weight: 600;
    padding: 1px 7px; border-radius: 8px; margin-left: 6px;
    background: rgba(255,200,100,0.15); border: 1px solid rgba(255,200,100,0.35);
    color: #ffc864; letter-spacing: 0.4px; vertical-align: middle;
    text-transform: uppercase;
  }
"""


# ── Obsidian knowledge source writer ─────────────────────────────────────────

def _write_knowledge_source(vault_path: Path, slug: str, title: str, summary: str, source_url: str, author: str, date_str: str) -> Path:
    """Create or update knowledge/sources/{slug}.md in the vault."""
    sources_dir = vault_path / "knowledge" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    out_path = sources_dir / f"{slug}.md"
    content = (
        f"---\n"
        f"title: {title}\n"
        f"source: {source_url}\n"
        f"author: {author}\n"
        f"date: {date_str}\n"
        f"ingested: {datetime.now(UTC).strftime('%Y-%m-%d')}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"## Summary\n\n"
        f"{summary}\n"
    )
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ── Main processing ───────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80]


def process_raw_note(
    md_path: Path,
    html_path: Path,
    vault_path: Path,
    state: dict,
    state_path: Path,
) -> bool:
    """Process one raw note. Returns True if the note was ingested (or skipped-as-thin)."""
    import llm

    text = md_path.read_text(encoding="utf-8")
    fm_block, body = _split_frontmatter(text)

    if not fm_block:
        print(f"  [skip] No frontmatter: {md_path.name}")
        return False

    if _is_ingested(fm_block):
        return False  # already done

    # Extract metadata
    title = ""
    h1_match = re.search(r'^# (.+)$', body, re.MULTILINE)
    if h1_match:
        title = h1_match.group(1).strip()
    if not title:
        title = md_path.stem

    source_url = _get_fm_value(fm_block, "source")
    author = _get_fm_value(fm_block, "author") or _get_fm_value(fm_block, "author_name") or "Unknown"
    date_str = _get_fm_value(fm_block, "date")
    knowledge_source = _get_fm_value(fm_block, "knowledge_source")
    source_badge = knowledge_source if knowledge_source else "Raw Note"

    # Validate or blank the ISO date (don't round-trip through pub-date parser)
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        date_str = ""

    # Skip thin content but still mark as ingested
    body_stripped = body.strip()
    if len(body_stripped) < MIN_BODY_CHARS:
        print(f"  [thin] Skipping (body {len(body_stripped)} chars): {md_path.name}")
        _mark_ingested(md_path, fm_block, body)
        return True

    print(f"\n  → {md_path.name}")
    print(f"    Title: {title}")

    # Clip content for LLM (use body without frontmatter)
    content = body_stripped[:24_000]

    # Step 1: 20-point summary
    print("    Generating summary...", flush=True)
    messages = _build_summary_messages(title, content)
    summary = llm.chat(messages, max_tokens=1200, temperature=0.1)
    print(f"    Summary: {len(summary)} chars")

    # Step 2: Knowledge source page
    slug = _slugify(title)
    knowledge_path = _write_knowledge_source(vault_path, slug, title, summary, source_url, author, date_str)
    print(f"    ✓ Knowledge source: {knowledge_path.name}")

    # Step 3: HTML card
    themes = _all_themes(state)
    theme_keywords = [kw for kw, _ in themes]
    card_schema = _build_raw_card_schema(theme_keywords)
    card_messages = _build_card_messages(title, date_str or "Unknown", summary, themes)

    print("    Generating HTML card...", flush=True)
    try:
        card = llm.chat_json(card_messages, schema=card_schema, max_tokens=512, temperature=0.1)
    except Exception as exc:
        print(f"    ✗ Card LLM failed: {exc}")
        _mark_ingested(md_path, fm_block, body)
        return True

    card.setdefault("title", title)
    card["iso_date"] = date_str
    card["summary_full"] = summary
    card["source_badge"] = source_badge

    theme_kw = card.get("theme_keyword", "")

    # Dynamic theme: if LLM proposed a new slug not in existing themes
    if theme_kw and theme_kw not in theme_keywords:
        new_label = card.get("new_theme_label", "") or f"🔖 {theme_kw.capitalize()}"
        new_desc = card.get("new_theme_desc", "") or f"Content related to {theme_kw}."
        new_accent = _pick_dynamic_accent(state)
        print(f"    New theme: {theme_kw} ({new_label})")
        _add_extra_theme(state, theme_kw, new_label, new_desc, new_accent)
        _save_state(state_path, state)
        # Ensure section + filter button in HTML before inserting card
        if html_path.exists():
            _ensure_theme_section_in_html(html_path, theme_kw, new_label, new_desc, new_accent)
    elif not theme_kw:
        card["theme_keyword"] = "modelos"  # fallback

    # Step 4: Insert into HTML mindmap
    if html_path.exists():
        inserted = _insert_raw_card_into_html(html_path, card, state)
        if inserted:
            print(f"    ✓ Card inserted (theme: {card['theme_keyword']})")
        else:
            print(f"    [skip] Card already in HTML")

    # Step 5: Mark ingested
    _mark_ingested(md_path, fm_block, body)
    print(f"    ✓ Marked ingested: {md_path.name}")
    return True


def main() -> None:
    raw_path_str = os.environ.get("OBSIDIAN_RAW_PATH", "").strip()
    if not raw_path_str:
        print("Error: OBSIDIAN_RAW_PATH not set in .env")
        sys.exit(1)

    raw_dir = Path(raw_path_str)
    if not raw_dir.exists():
        print(f"Error: raw directory not found: {raw_dir}")
        sys.exit(1)

    transcriptions_path = os.environ.get("OBSIDIAN_TRANSCRIPTIONS_PATH", "").strip()
    if not transcriptions_path:
        print("Error: OBSIDIAN_TRANSCRIPTIONS_PATH not set in .env")
        sys.exit(1)

    transcriptions_dir = Path(transcriptions_path)
    html_path = transcriptions_dir / "AI Daily Brief - Mapa Mental.html"

    # Vault root is <transcriptions_dir>/../../.. (projects/SpotifyTranscript/Transcriptions)
    vault_path_str = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if vault_path_str:
        vault_path = Path(vault_path_str)
    else:
        try:
            vault_path = transcriptions_dir.parents[2]
        except IndexError:
            print("Error: Cannot derive vault path. Set OBSIDIAN_VAULT_PATH in .env")
            sys.exit(1)

    state_path = Path(__file__).parent / "state.json"
    state = _load_state(state_path)

    md_files = sorted(raw_dir.glob("*.md"))
    pending = []
    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8")
        except Exception:
            continue
        fm_block, _ = _split_frontmatter(text)
        if fm_block and not _is_ingested(fm_block):
            pending.append(md_path)

    if not pending:
        print("raw_ingest: nothing to do (all notes already ingested)")
        return

    print(f"raw_ingest: {len(pending)} note(s) to process from {raw_dir}")

    processed = 0
    failed = 0
    for md_path in pending:
        try:
            did_work = process_raw_note(md_path, html_path, vault_path, state, state_path)
            if did_work:
                processed += 1
        except Exception as exc:
            print(f"  ✗ Failed {md_path.name}: {exc}")
            failed += 1

    print(f"\nraw_ingest done. {processed} note(s) ingested, {failed} failed.")
    print(f"Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")


if __name__ == "__main__":
    main()
