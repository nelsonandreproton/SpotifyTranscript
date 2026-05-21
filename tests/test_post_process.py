"""Tests for post_process.py — frontmatter, summary insertion, HTML card insertion."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

import post_process as pp


# ── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_MD = dedent("""\
    ---
    title: "Test Episode"
    show: "The AI Daily Brief"
    spotify_url: ""
    published: "May 07, 2026 4:21pm"
    transcribed_at: "2026-05-08 08:45 UTC"
    tags:
      - podcast
      - transcript
    ---

    # Test Episode

    **Show:** The AI Daily Brief

    ---

    ## Transcript

    This is the transcript text. It covers many topics including AI, agents, and more.
    The second sentence adds depth. Third sentence concludes the introduction.
""")

SAMPLE_MD_SUMMARIZED = SAMPLE_MD.replace(
    "transcribed_at: \"2026-05-08 08:45 UTC\"\n",
    "transcribed_at: \"2026-05-08 08:45 UTC\"\nsummarized: true\n",
)

SAMPLE_HTML = dedent("""\
    <!DOCTYPE html>
    <html>
    <body>
    <div class="mindmap">
      <div class="central-node"><h2>Test</h2></div>

      <div class="theme-section">
        <h3>🤖 Modelos</h3>
      </div>

      <div class="article-card" data-tags="modelos" style="--accent:#00f2fe">
        <h4 class="article-title">Old Card</h4>
      </div>

      <div class="theme-section">
        <h3>💰 Modelo de Negócio</h3>
      </div>

    </div>
    </body>
    </html>
""")


# ── Frontmatter helpers ───────────────────────────────────────────────────────

class TestSplitFrontmatter:
    def test_splits_correctly(self):
        fm, body = pp._split_frontmatter(SAMPLE_MD)
        assert fm.startswith("---\n")
        assert fm.endswith("---\n")
        assert "# Test Episode" in body

    def test_no_frontmatter(self):
        text = "just plain text"
        fm, body = pp._split_frontmatter(text)
        assert fm == ""
        assert body == text


class TestGetFmValue:
    def test_reads_title(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        assert pp._get_fm_value(fm, "title") == "Test Episode"

    def test_reads_show(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        assert pp._get_fm_value(fm, "show") == "The AI Daily Brief"

    def test_missing_key_returns_empty(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        assert pp._get_fm_value(fm, "nonexistent") == ""


class TestSetFmFlag:
    def test_adds_new_flag(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        updated = pp._set_fm_flag(fm, "summarized", "true")
        assert "summarized: true" in updated

    def test_updates_existing_flag(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        fm_with_flag = pp._set_fm_flag(fm, "summarized", "false")
        fm_updated = pp._set_fm_flag(fm_with_flag, "summarized", "true")
        assert "summarized: true" in fm_updated
        assert fm_updated.count("summarized:") == 1

    def test_preserves_other_fields(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        updated = pp._set_fm_flag(fm, "summarized", "true")
        assert 'title: "Test Episode"' in updated
        assert 'show: "The AI Daily Brief"' in updated


class TestIsSummarized:
    def test_not_summarized(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD)
        assert not pp._is_summarized(fm)

    def test_summarized(self):
        fm, _ = pp._split_frontmatter(SAMPLE_MD_SUMMARIZED)
        assert pp._is_summarized(fm)


# ── Summary insertion ─────────────────────────────────────────────────────────

BULLET_SUMMARY = "\n".join(f"{i}. Point number {i}." for i in range(1, 21))


class TestInsertSummary:
    def test_inserts_before_transcript(self):
        _, body = pp._split_frontmatter(SAMPLE_MD)
        new_body = pp._insert_summary(body, BULLET_SUMMARY)
        transcript_pos = new_body.index("## Transcript")
        summary_pos = new_body.index("## Summary")
        assert summary_pos < transcript_pos

    def test_summary_content_present(self):
        _, body = pp._split_frontmatter(SAMPLE_MD)
        new_body = pp._insert_summary(body, BULLET_SUMMARY)
        assert "Point number 1." in new_body
        assert "Point number 20." in new_body

    def test_idempotent_no_duplicate(self):
        _, body = pp._split_frontmatter(SAMPLE_MD)
        once = pp._insert_summary(body, BULLET_SUMMARY)
        twice = pp._insert_summary(once, BULLET_SUMMARY)
        assert twice.count("## Summary") == 1

    def test_replaces_existing_summary(self):
        _, body = pp._split_frontmatter(SAMPLE_MD)
        first = pp._insert_summary(body, "1. Old point.")
        second = pp._insert_summary(first, "1. New point.")
        assert "New point." in second
        assert "Old point." not in second


# ── HTML card insertion ───────────────────────────────────────────────────────

SAMPLE_CARD = {
    "theme_keyword": "modelos",
    "date_pt": "07 Mai 2026",
    "title": "New Test Card",
    "summary_pt": "Resumo do episódio em português.",
    "key_points": ["Ponto 1.", "Ponto 2.", "Ponto 3."],
    "tags": ["AI", "Agents"],
}


class TestInsertCardIntoHtml:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.html_path = self.tmpdir / "test.html"
        self.html_path.write_text(SAMPLE_HTML, encoding="utf-8")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_card_inserted(self):
        pp._insert_card_into_html(self.html_path, SAMPLE_CARD)
        html = self.html_path.read_text(encoding="utf-8")
        assert "New Test Card" in html
        assert "Resumo do episódio em português." in html

    def test_backup_created(self):
        pp._insert_card_into_html(self.html_path, SAMPLE_CARD)
        assert self.html_path.with_suffix(".html.bak").exists()

    def test_card_before_next_theme_section(self):
        pp._insert_card_into_html(self.html_path, SAMPLE_CARD)
        html = self.html_path.read_text(encoding="utf-8")
        new_card_pos = html.index("New Test Card")
        negocio_pos = html.index("Modelo de Negócio")
        assert new_card_pos < negocio_pos

    def test_old_card_preserved(self):
        pp._insert_card_into_html(self.html_path, SAMPLE_CARD)
        html = self.html_path.read_text(encoding="utf-8")
        assert "Old Card" in html

    def test_tags_rendered(self):
        pp._insert_card_into_html(self.html_path, SAMPLE_CARD)
        html = self.html_path.read_text(encoding="utf-8")
        assert '<span class="tag">AI</span>' in html


# ── process_file integration (mocked LLM) ────────────────────────────────────

class TestProcessFile:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.md_path = self.tmpdir / "2026-05-07 Test Episode.md"
        self.md_path.write_text(SAMPLE_MD, encoding="utf-8")
        self.html_path = self.tmpdir / "AI Daily Brief - Mapa Mental.html"
        self.html_path.write_text(SAMPLE_HTML, encoding="utf-8")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_summary_written_to_file(self):
        card_json = json.dumps({
            "theme_keyword": "modelos",
            "date_pt": "07 Mai 2026",
            "title": "Test Episode",
            "summary_pt": "Resumo.",
            "key_points": ["A.", "B."],
            "tags": ["AI"],
        })
        with patch("llm.chat", side_effect=[BULLET_SUMMARY, card_json]) as mock_chat, \
             patch("llm.chat_json", return_value=json.loads(card_json)):
            pp.process_file(self.md_path, self.html_path)

        content = self.md_path.read_text(encoding="utf-8")
        assert "## Summary" in content
        assert "Point number 1." in content

    def test_summarized_flag_set(self):
        card_json = {
            "theme_keyword": "modelos",
            "date_pt": "07 Mai 2026",
            "title": "Test Episode",
            "summary_pt": "Resumo.",
            "key_points": ["A.", "B."],
            "tags": ["AI"],
        }
        with patch("llm.chat", return_value=BULLET_SUMMARY), \
             patch("llm.chat_json", return_value=card_json):
            pp.process_file(self.md_path, self.html_path)

        content = self.md_path.read_text(encoding="utf-8")
        fm, _ = pp._split_frontmatter(content)
        assert pp._is_summarized(fm)

    def test_skips_already_summarized(self):
        self.md_path.write_text(SAMPLE_MD_SUMMARIZED, encoding="utf-8")
        with patch("llm.chat") as mock_chat:
            pp.process_file(self.md_path, self.html_path)
            mock_chat.assert_not_called()

    def test_html_updated_for_ai_daily_brief(self):
        card_json = {
            "theme_keyword": "modelos",
            "date_pt": "07 Mai 2026",
            "title": "Test Episode",
            "summary_pt": "Resumo.",
            "key_points": ["A.", "B."],
            "tags": ["AI"],
        }
        with patch("llm.chat", return_value=BULLET_SUMMARY), \
             patch("llm.chat_json", return_value=card_json):
            pp.process_file(self.md_path, self.html_path)

        html = self.html_path.read_text(encoding="utf-8")
        assert "Test Episode" in html
