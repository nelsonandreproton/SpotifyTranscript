"""Transcribe an MP3 file using faster-whisper (local, free, CPU-based)."""

import os
from faster_whisper import WhisperModel

VALID_MODELS = frozenset({
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large-v1", "large-v2", "large-v3",
})

_model_cache: dict[str, WhisperModel] = {}


def _get_model(model_name: str) -> WhisperModel:
    if model_name not in _model_cache:
        _model_cache[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _model_cache[model_name]


def _format_bar(current: float, total: float, width: int = 30) -> str:
    pct = min(current / total, 1.0) if total > 0 else 0
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    elapsed = _fmt_time(current)
    duration = _fmt_time(total)
    return f"[{bar}] {pct*100:5.1f}% — {elapsed} / {duration}"


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def transcribe(audio_path: str) -> str:
    """
    Transcribe audio_path and return the full transcript as a string.
    Shows a live progress bar as segments are processed.
    Model is read from WHISPER_MODEL env var (default: medium.en).
    """
    model_name = os.environ.get("WHISPER_MODEL", "medium.en")
    if model_name not in VALID_MODELS:
        raise ValueError(
            f"Invalid WHISPER_MODEL {model_name!r}. Valid options: {sorted(VALID_MODELS)}"
        )

    print(f"      Model   : {model_name}")
    model = _get_model(model_name)

    segments, info = model.transcribe(audio_path, language="en", beam_size=5)
    duration = info.duration
    print(f"      Duration: {_fmt_time(duration)}")
    print(f"      Language: {info.language} ({info.language_probability:.0%})")
    print()

    parts = []
    for segment in segments:
        parts.append(segment.text.strip())
        bar = _format_bar(segment.end, duration)
        print(f"\r      {bar}", end="", flush=True)

    print()  # newline after progress bar
    return " ".join(parts)
