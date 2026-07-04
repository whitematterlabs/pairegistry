"""Cloud voice provider — engine-blind surface the web dispatcher calls.

Implements the same contract as the local `voice` package:

    transcribe(audio, *, content_type, filename, language=None, prompt=None) -> str
    synthesize(text, *, voice_id=None, speed=None) -> (bytes, mime)

STT = OpenAI transcriptions; TTS = ElevenLabs. Keys come from the environment
(`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`), loaded from $PAI_ROOT/.env(.local) by
boot at kernel start; `_reload_dotenv` re-reads them so a freshly dropped key
works without a restart. Keys never reach the browser — only this server-side
module touches them.
"""

from __future__ import annotations

import os

import requests

from boot import paths


# ── ElevenLabs (TTS) ─────────────────────────────────────────────────────────
_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
_DEFAULT_MODEL_ID = "eleven_turbo_v2_5"  # low-latency

# ── OpenAI (STT) ─────────────────────────────────────────────────────────────
_OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


def _reload_dotenv() -> None:
    """Re-read .env.local / .env from $PAI_ROOT, same order as boot/__init__.py.

    Lets the user drop a fresh key into the file mid-session and have voice work
    on the next request without restarting. override=False matches boot so a
    shell-exported value still wins.
    """
    from dotenv import load_dotenv

    load_dotenv(paths.PAI_ROOT / ".env.local")
    load_dotenv(paths.PAI_ROOT / ".env")


def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    speed: float | None = None,
) -> tuple[bytes, str]:
    """Turn text into playable audio via ElevenLabs. Returns (mp3 bytes, mime)."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        _reload_dotenv()
        api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    chosen_voice = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or _DEFAULT_VOICE_ID
    model_id = os.environ.get("ELEVENLABS_MODEL_ID") or _DEFAULT_MODEL_ID
    payload: dict = {"text": text, "model_id": model_id}
    if speed is not None:
        payload["voice_settings"] = {"speed": max(0.7, min(1.2, float(speed)))}

    resp = requests.post(
        _ELEVENLABS_TTS_URL.format(voice_id=chosen_voice),
        headers={"xi-api-key": api_key, "accept": "audio/mpeg"},
        params={"output_format": "mp3_44100_128"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content, "audio/mpeg"


def transcribe(
    audio: bytes,
    *,
    content_type: str,
    filename: str,
    language: str | None = None,
    prompt: str | None = None,
) -> str:
    """Turn recorded browser audio into text via OpenAI transcriptions."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _reload_dotenv()
        api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    if not audio:
        raise ValueError("empty audio")

    model = os.environ.get("OPENAI_TRANSCRIBE_MODEL") or _DEFAULT_TRANSCRIBE_MODEL
    data = {"model": model, "response_format": "json"}
    configured_language = language or os.environ.get("OPENAI_TRANSCRIBE_LANGUAGE")
    configured_prompt = prompt or os.environ.get("OPENAI_TRANSCRIBE_PROMPT")
    if configured_language:
        data["language"] = configured_language
    if configured_prompt:
        data["prompt"] = configured_prompt

    resp = requests.post(
        _OPENAI_TRANSCRIPTIONS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files={"file": (filename, audio, content_type)},
        timeout=60,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError("transcription response was not JSON") from e
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("transcription response missing text")
    return text.strip()
