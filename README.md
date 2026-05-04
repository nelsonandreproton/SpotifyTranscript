# SpotifyTranscript

Transcribe Spotify podcast episodes to markdown files in your Obsidian vault. Free, local, no subscriptions.

## How it works

1. Resolves episode metadata from Spotify (no auth needed)
2. Finds the RSS feed via [PodcastIndex.org](https://podcastindex.org)
3. Downloads the MP3 from the RSS feed
4. Transcribes locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — shows live progress bar
5. Saves a `.md` file with frontmatter and transcript to your Obsidian vault

> **Note:** Works for any podcast distributed via RSS. Will not work for Spotify-exclusive content.

## First-time setup

### 1. Python environment

```bash
cd C:\DEV\SpotifyTranscript
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. PodcastIndex API keys

Register for free at [api.podcastindex.org](https://api.podcastindex.org) to get your API key and secret.

### 3. Create .env

Copy `.env.example` to `.env` and fill in your values:

```bash
copy .env.example .env
```

Required:
```env
PODCASTINDEX_API_KEY=your_api_key_here
PODCASTINDEX_API_SECRET=your_api_secret_here
OBSIDIAN_TRANSCRIPTIONS_PATH=C:\DEV\Obsidian\Nelson\projects\SpotifyTranscript\Transcriptions
```

Optional:
```env
WHISPER_MODEL=medium.en
HF_TOKEN=your_hf_token_here
HF_HUB_DISABLE_SYMLINKS_WARNING=1
```

### 4. First run (Whisper model download)

The first run downloads the Whisper model (~1.4 GB for `medium.en` in CTranslate2 format). It is cached locally after that at `~/.cache/huggingface/hub/`.

## Usage

```bash
cd C:\DEV\SpotifyTranscript
.venv\Scripts\activate
python transcribe.py https://open.spotify.com/episode/<episode_id>
```

Output is saved to your Obsidian vault at:
`C:\DEV\Obsidian\Nelson\projects\SpotifyTranscript\Transcriptions\<Episode Title>.md`

### Output format

The markdown file contains:
- YAML frontmatter (title, show, Spotify URL, date, transcription timestamp)
- **Transcript** section — full transcript in readable paragraphs

## Whisper models

| Model | Cache size | Speed (30 min ep) | Quality |
|---|---|---|---|
| `tiny.en` | ~150 MB | ~1 min | Low |
| `base.en` | ~290 MB | ~2 min | OK |
| `medium.en` | ~1.4 GB | ~19 min (Core Ultra 7) | **Good (default)** |
| `large-v3` | ~3 GB | ~45 min | Best |

Change the model in `.env` via `WHISPER_MODEL=`.

> Cache sizes are in CTranslate2 format (faster-whisper), which is larger than the original OpenAI Whisper format.
