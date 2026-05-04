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


def transcribe(audio_path: str) -> str:
    """
    Transcribe audio_path and return the full transcript as a string.
    Model is read from WHISPER_MODEL env var (default: medium.en).
    """
    model_name = os.environ.get("WHISPER_MODEL", "medium.en")
    if model_name not in VALID_MODELS:
        raise ValueError(
            f"Invalid WHISPER_MODEL {model_name!r}. Valid options: {sorted(VALID_MODELS)}"
        )

    print(f"Loading Whisper model: {model_name}")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    print(f"Transcribing: {audio_path}")
    segments, info = model.transcribe(audio_path, language="en", beam_size=5)

    print(f"Detected language: {info.language} (probability {info.language_probability:.2f})")

    parts = []
    for segment in segments:
        parts.append(segment.text.strip())

    return " ".join(parts)
