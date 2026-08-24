import base64
import json
import re
from typing import Any

import numpy as np
from google import genai

from llm.base import (
    Capabilities,
    GeneratedAudio,
    GenerationRequest,
    GenerationResult,
    MusicProvider,
    TimedLyric,
)
from workspaces.transcript import cues_from_lyric_text

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_LYRIA_SAMPLERATE = 44100
_LYRIA_CHANNELS = 2


def _display_name_for_model(model_id: str) -> str:
    lower = model_id.lower()
    if "clip" in lower:
        return "Lyria 3 Clip"
    if "pro" in lower:
        return "Lyria 3 Pro"
    return "Lyria 3"


class Lyria3Provider(MusicProvider):
    def __init__(self, settings, *, model_id: str) -> None:
        self.model_id = model_id
        self.display_name = _display_name_for_model(model_id)
        self._settings = settings

    def _supports_wav(self) -> bool:
        return "pro" in self.model_id.lower()

    def capabilities(self) -> Capabilities:
        mimes = ("audio/wav", "audio/mpeg") if self._supports_wav() else ("audio/mpeg",)
        return Capabilities(
            accepts_text=True,
            accepts_images=True,
            returns_text=True,
            returns_lyrics=True,
            output_mimes=mimes,
        )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        api_key = self._settings.resolved_gemini_api_key()
        if not api_key:
            raise RuntimeError(
                "Lyria 3 needs a Gemini API key. Set GEMINI_API_KEY or add it in Preferences."
            )
        payload = _interaction_input(request)
        create_kwargs: dict[str, Any] = {
            "model": self.model_id,
            "input": payload,
        }
        fmt = str(request.extra.get("response_format", "wav")).lower()
        want_mp3 = fmt in {"mp3", "audio/mpeg", "audio/mp3"}
        # Lyria 3 Pro: response_format={"type": "audio"} selects WAV.
        # Clip stays on the default MP3 output.
        if self._supports_wav() and not want_mp3:
            create_kwargs["response_format"] = {"type": "audio"}

        client = genai.Client(api_key=api_key)
        interactions = getattr(client, "interactions", None)
        if interactions is None or not hasattr(interactions, "create"):
            raise RuntimeError(
                "This google-genai version has no client.interactions.create. "
                "Upgrade google-genai to use Lyria 3."
            )
        interaction = interactions.create(**create_kwargs)

        texts, audios = _parse_interaction(interaction)
        if not audios:
            raise RuntimeError("Lyria 3 returned no audio.")
        joined = "\n".join(texts).strip() or None
        lyrics = _lyrics_from_text(joined)
        raw: dict[str, Any] | None
        try:
            raw = interaction.model_dump() if hasattr(interaction, "model_dump") else None
        except Exception:
            raw = None
        return GenerationResult(audios=audios, text=joined, lyrics=lyrics, raw=raw)


def _interaction_input(request: GenerationRequest) -> str | list[dict[str, Any]]:
    """Build Interactions API `input` (string, or text/image content list)."""
    parts: list[dict[str, Any]] = []
    if request.history:
        last_user = ""
        for turn in request.history:
            if turn.role != "user":
                continue
            last_user = (turn.text or "").strip()
            _append_text_part(parts, turn.text)
            if turn.negative_prompt:
                _append_text_part(
                    parts, f"Negative prompt (avoid): {turn.negative_prompt}"
                )
            _append_image_parts(parts, turn.images, turn.image_mimes)
        current = request.prompt.strip()
        if current and current != last_user:
            _append_text_part(parts, current)
            if request.negative_prompt:
                _append_text_part(
                    parts, f"Negative prompt (avoid): {request.negative_prompt}"
                )
            _append_image_parts(parts, request.images, request.image_mimes)
    else:
        _append_text_part(parts, request.prompt)
        if request.negative_prompt:
            _append_text_part(
                parts, f"Negative prompt (avoid): {request.negative_prompt}"
            )
        _append_image_parts(parts, request.images, request.image_mimes)
    if not parts:
        raise RuntimeError("Nothing to send to Lyria 3.")
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _append_text_part(parts: list[dict[str, Any]], text: str | None) -> None:
    stripped = (text or "").strip()
    if stripped:
        parts.append({"type": "text", "text": stripped})


def _append_image_parts(
    parts: list[dict[str, Any]], images: list[bytes], image_mimes: list[str]
) -> None:
    mimes = image_mimes or ["image/png"] * len(images)
    for image_bytes, mime in zip(images, mimes, strict=False):
        if not image_bytes:
            continue
        parts.append(
            {
                "type": "image",
                "mime_type": mime or "image/png",
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        )


def _parse_interaction(interaction: Any) -> tuple[list[str], list[GeneratedAudio]]:
    texts: list[str] = []
    audios: list[GeneratedAudio] = []
    for block in _iter_content_blocks(interaction):
        block_type = str(getattr(block, "type", None) or "").lower()
        if isinstance(block, dict):
            block_type = str(block.get("type") or "").lower()
        if block_type == "audio":
            audio = _generated_from_audio_block(block)
            if audio:
                audios.append(audio)
        elif block_type == "text":
            text = _block_text(block)
            if text:
                texts.append(text)
    if not audios:
        try:
            generated = getattr(interaction, "output_audio", None)
        except TypeError:
            generated = None
        audio = _generated_from_audio_block(generated)
        if audio:
            audios.append(audio)
    if not texts:
        output_text = getattr(interaction, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            texts.append(output_text.strip())
    return texts, audios


def _iter_content_blocks(interaction: Any):
    steps = getattr(interaction, "steps", None) or []
    yielded = False
    for step in steps:
        step_type = getattr(step, "type", None)
        if isinstance(step, dict):
            step_type = step.get("type")
        if step_type != "model_output":
            continue
        content = getattr(step, "content", None)
        if isinstance(step, dict):
            content = step.get("content")
        for block in content or []:
            yielded = True
            yield block
    if yielded:
        return
    for item in getattr(interaction, "outputs", None) or []:
        yield item


def _block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        text = block.get("text")
    else:
        text = getattr(block, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _generated_from_audio_block(block: Any) -> GeneratedAudio | None:
    if block is None:
        return None
    if isinstance(block, dict):
        data = block.get("data")
        mime = block.get("mime_type") or block.get("mimeType")
    else:
        data = getattr(block, "data", None)
        mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", None)
    payload = _as_audio_bytes(data)
    if not payload:
        return None
    return GeneratedAudio(
        data=payload,
        mime=_audio_mime(payload, mime),
        samplerate=_LYRIA_SAMPLERATE,
        channels=_LYRIA_CHANNELS,
    )


def _as_audio_bytes(data: Any) -> bytes:
    if data is None:
        return b""
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw[:4] == b"RIFF" or raw[:3] == b"ID3":
            return raw
        try:
            decoded = base64.b64decode(raw, validate=True)
            return decoded or raw
        except Exception:
            return raw
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("RIFF") or stripped.startswith("ID3"):
            return stripped.encode("latin-1")
        return base64.b64decode(stripped)
    return b""


def _audio_mime(payload: bytes, reported: Any) -> str:
    if payload[:4] == b"RIFF":
        return "audio/wav"
    mime = str(reported or "").lower()
    if "wav" in mime:
        return "audio/wav"
    if mime:
        return str(reported)
    return "audio/mpeg"


def _lyrics_from_text(text: str | None) -> list[TimedLyric]:
    if not text:
        return []
    parsed = _try_json_lyrics(text)
    if parsed:
        return parsed
    cues = cues_from_lyric_text(text)
    return [
        TimedLyric(start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text) for cue in cues
    ]


def _try_json_lyrics(text: str) -> list[TimedLyric]:
    match = _JSON_BLOCK.search(text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = None
    if isinstance(data, dict):
        for key in ("lyrics", "timed_lyrics", "cues", "lines"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    elif isinstance(data, list):
        items = data
    if not items:
        return []
    lyrics: list[TimedLyric] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text_value = item.get("text") or item.get("lyric") or item.get("content")
        if not text_value:
            continue
        start = item.get("start_ms", item.get("startMs", item.get("start")))
        end = item.get("end_ms", item.get("endMs", item.get("end")))
        if start is None or end is None:
            continue
        lyrics.append(
            TimedLyric(
                start_ms=_to_ms(start),
                end_ms=_to_ms(end),
                text=str(text_value),
            )
        )
    return lyrics


def _to_ms(value) -> int:
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        parts = [p.replace(",", ".") for p in parts]
        if len(parts) == 3:
            h, m, s = parts
            return int(np.floor((float(h) * 3600 + float(m) * 60 + float(s)) * 1000))
        if len(parts) == 2:
            m, s = parts
            return int(np.floor((float(m) * 60 + float(s)) * 1000))
    return int(np.floor(float(value) * (1000 if float(value) < 1000 else 1)))
