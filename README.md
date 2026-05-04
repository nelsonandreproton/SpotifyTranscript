# SpotifyTranscript

Transcribe Spotify podcast episodes to markdown files in your Obsidian vault. Free, local, no subscriptions.

## How it works

1. Resolves episode metadata from Spotify (no auth needed)
2. Finds the RSS feed via [PodcastIndex.org](https://podcastindex.org)
3. Downloads the MP3 from the RSS feed
4. Transcribes locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
5. Saves a `.md` file with frontmatter to your Obsidian vault

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

Register for free at [podcastindex.org/developer](https://api.podcastindex.org) to get your API key and secret.

### 3. Create .env

Copy `.env.example` to `.env` and fill in your values:

```bash
copy .env.example .env
```

```env
PODCASTINDEX_API_KEY=your_api_key_here
PODCASTINDEX_API_SECRET=your_api_secret_here
OBSIDIAN_TRANSCRIPTIONS_PATH=C:\DEV\Obsidian\Nelson\projects\SpotifyTranscript\Transcriptions
WHISPER_MODEL=medium.en
```

### 4. First run (Whisper model download)

The first run will download the Whisper model (~500 MB for `medium.en`). It is cached locally after that.

## Usage

```bash
cd C:\DEV\SpotifyTranscript
.venv\Scripts\activate
python transcribe.py https://open.spotify.com/episode/<episode_id>
```

Output is saved to your Obsidian vault at:
`C:\DEV\Obsidian\Nelson\projects\SpotifyTranscript\Transcriptions\<Episode Title>.md`

## Whisper models

| Model | Size | Speed (30 min) | Quality |
|---|---|---|---|
| `tiny.en` | 75 MB | ~1 min | Low |
| `base.en` | 145 MB | ~2 min | OK |
| `medium.en` | 500 MB | ~5-8 min | **Good (default)** |
| `large-v3` | 1.5 GB | ~15 min | Best |

Change the model in `.env` via `WHISPER_MODEL=`.
