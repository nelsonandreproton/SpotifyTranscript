"""Generate a podcast episode summary via LM Studio (local LLM, streaming)."""

import os
import sys
import requests

LMSTUDIO_BASE = os.environ.get("LMSTUDIO_URL", "http://169.254.83.107:1234") + "/v1"
LMSTUDIO_TIMEOUT_CONNECT = 3  # seconds — fast fail if not running

SUMMARY_PROMPT = """You are summarizing a podcast episode transcript. The episode has three parts:
1. The day's most important news
2. Sponsor messages (ignore these)
3. The episode's main topic deep-dive

Write a structured summary with these sections:

## Key News
- Bullet points of the most important news items mentioned

## Main Topic
A 2-3 paragraph summary of the deep-dive topic, capturing the key ideas and arguments

## Takeaways
- 3-5 actionable or memorable takeaways from the episode

Be concise and factual. Skip any sponsor or advertisement content.

Transcript:
"""


def is_lmstudio_running() -> bool:
    """Return True if LM Studio API is reachable."""
    try:
        resp = requests.get(f"{LMSTUDIO_BASE}/models", timeout=LMSTUDIO_TIMEOUT_CONNECT)
        return resp.status_code == 200
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.Timeout:
        return False


def ask_user_to_start() -> bool:
    """Prompt user to start LM Studio. Returns True if they want to retry."""
    print("\n  LM Studio is not running.")
    print("  Options:")
    print("    [s] Start LM Studio, then press Enter to continue")
    print("    [n] Skip summary")
    choice = input("  Choice [s/n]: ").strip().lower()
    if choice == "s":
        input("  Press Enter once LM Studio is running and the model is loaded...")
        return True
    return False


def get_model_name() -> str:
    return os.environ.get("LMSTUDIO_MODEL", "qwen2.5-7b-instruct-1m")


def summarize(transcript: str) -> str | None:
    """
    Generate a summary of the transcript using LM Studio.
    Returns the summary string, or None if skipped.
    Streams output to stdout as it generates.
    """
    # Check availability — offer retry once
    if not is_lmstudio_running():
        retry = ask_user_to_start()
        if not retry or not is_lmstudio_running():
            print("      Skipping summary.")
            return None

    model = get_model_name()
    prompt = SUMMARY_PROMPT + transcript

    print(f"      Model   : {model}")
    print("      Generating summary (streaming)...\n")
    print("─" * 60)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    summary_parts = []
    try:
        with requests.post(
            f"{LMSTUDIO_BASE}/chat/completions",
            json=payload,
            stream=True,
            timeout=(LMSTUDIO_TIMEOUT_CONNECT, 120),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    import json
                    chunk = json.loads(data)
                    token = chunk["choices"][0]["delta"].get("content", "")
                    if token:
                        print(token, end="", flush=True)
                        summary_parts.append(token)
    except requests.exceptions.RequestException as e:
        print(f"\n      LM Studio error: {e}")
        return None

    print("\n" + "─" * 60)
    return "".join(summary_parts)
