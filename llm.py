"""Unified LLM client: NVIDIA NIM primary, local Qwen2.5-7B GGUF fallback."""

from __future__ import annotations

import json
import os
from typing import Any

_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NIM_MODEL = "meta/llama-3.3-70b-instruct"
_LOCAL_MODEL_DEFAULT = r".\models\Qwen2.5-7B-Instruct-Q4_K_M.gguf"
_LOCAL_N_CTX = 12288

# Cached backend after first successful init
_backend: str | None = None
_nim_client: Any = None
_local_llm: Any = None


def _init_nim() -> bool:
    """Try to initialise the NIM OpenAI client. Returns True on success."""
    global _nim_client
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        return False
    try:
        from openai import OpenAI
        _nim_client = OpenAI(base_url=_NIM_BASE_URL, api_key=api_key)
        # Lightweight probe — list models
        _nim_client.models.list()
        return True
    except Exception:
        _nim_client = None
        return False


def _init_local() -> bool:
    """Load local Qwen GGUF model. Returns True on success."""
    global _local_llm
    model_path = os.environ.get("LOCAL_MODEL_PATH", _LOCAL_MODEL_DEFAULT)
    if not os.path.exists(model_path):
        return False
    try:
        from llama_cpp import Llama
        _local_llm = Llama(
            model_path=model_path,
            n_ctx=_LOCAL_N_CTX,
            n_gpu_layers=0,
            chat_format="chatml",
            verbose=False,
        )
        return True
    except Exception:
        _local_llm = None
        return False


def _ensure_backend() -> str:
    """Pick and cache a backend. Raises RuntimeError if neither is available."""
    global _backend
    if _backend:
        return _backend
    print("  [llm] Connecting to NVIDIA NIM...", flush=True)
    if _init_nim():
        _backend = "nim"
        print(f"  [llm] Backend: NIM ({_NIM_MODEL})")
        return _backend
    print("  [llm] NIM unavailable — loading local Qwen model...", flush=True)
    if _init_local():
        model_path = os.environ.get("LOCAL_MODEL_PATH", _LOCAL_MODEL_DEFAULT)
        _backend = "local"
        print(f"  [llm] Backend: local ({model_path})")
        return _backend
    raise RuntimeError(
        "No LLM backend available. "
        "Set NVIDIA_API_KEY for NIM or LOCAL_MODEL_PATH pointing to a GGUF file."
    )


def chat(
    messages: list[dict[str, str]],
    max_tokens: int = 1024,
    temperature: float = 0.1,
    json_schema: dict | None = None,
) -> str:
    """
    Send a chat request to whichever backend is available.

    Args:
        messages:     OpenAI-style message list.
        max_tokens:   Maximum tokens to generate.
        temperature:  Sampling temperature.
        json_schema:  If provided, constrain output to this JSON schema.

    Returns:
        The assistant message content as a string.
    """
    backend = _ensure_backend()

    if backend == "nim":
        kwargs: dict[str, Any] = {
            "model": _NIM_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_object",
                "schema": json_schema,
            }
        resp = _nim_client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    # local backend
    kwargs = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_schema is not None:
        kwargs["response_format"] = {
            "type": "json_object",
            "schema": json_schema,
        }
    result = _local_llm.create_chat_completion(**kwargs)
    return result["choices"][0]["message"]["content"]


def chat_json(
    messages: list[dict[str, str]],
    schema: dict,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> dict:
    """Convenience wrapper: call chat() with a schema and parse the JSON response."""
    raw = chat(messages, max_tokens=max_tokens, temperature=temperature, json_schema=schema)
    return json.loads(raw)
