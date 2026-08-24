"""Translate LRC lyrics with a Gemini text model."""

from workspaces.transcript import Cue, dump_lrc, parse_lrc


def translate_lrc(
    cues: list[Cue],
    target_language: str,
    *,
    api_key: str,
    model_id: str,
    title: str | None = None,
    source_language: str | None = None,
) -> list[Cue]:
    """Translate lyric text while keeping LRC timestamps and [[section]] tags."""
    if not cues:
        raise ValueError("There is no transcript to translate.")
    source = dump_lrc(cues, title=title, language=source_language)
    prompt = (
        "Translate the lyric text in this LRC file into "
        f"{target_language}.\n\n"
        "Output rules (must follow):\n"
        "1. Output only LRC. No markdown, no commentary, no code fences.\n"
        "2. Keep every timestamp tag character-for-character "
        "(examples: [00:16.00], [01:02.35]).\n"
        "3. Keep metadata in double brackets unchanged, e.g. [[A0]] [[B1]].\n"
        "4. Keep LRC id tags such as [ti:], [ar:], [al:], [la:] unchanged.\n"
        "5. Translate only the sung or spoken words after the timestamps.\n"
        "6. One lyric line per input lyric line, same order. "
        "Do not add or remove timestamp lines.\n\n"
        f"{source}"
    )
    text = _generate_text(api_key=api_key, model_id=model_id, prompt=prompt)
    parsed = parse_lrc(_unwrap_fences(text))
    if not parsed:
        raise RuntimeError(
            "The translation model did not return valid LRC timestamps."
        )
    return _align_translated_cues(cues, parsed)


def _unwrap_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped[3:]
    if "\n" in stripped:
        first, rest = stripped.split("\n", 1)
        if first.strip().lower() in {"", "lrc", "lyric", "lyrics", "txt"}:
            stripped = rest
        else:
            stripped = f"{first}\n{rest}"
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3].rstrip()
    return stripped.strip()


def _align_translated_cues(original: list[Cue], translated: list[Cue]) -> list[Cue]:
    """Prefer original start times so rolling playback stays aligned."""
    if len(translated) == len(original):
        return [
            Cue(start_ms=src.start_ms, end_ms=src.end_ms, text=dst.text)
            for src, dst in zip(original, translated)
        ]
    by_start = {cue.start_ms: cue for cue in translated}
    aligned: list[Cue] = []
    for src in original:
        dst = by_start.get(src.start_ms)
        if dst is None:
            continue
        aligned.append(Cue(start_ms=src.start_ms, end_ms=src.end_ms, text=dst.text))
    if aligned:
        return aligned
    return translated


def _generate_text(*, api_key: str, model_id: str, prompt: str) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ImportError("google-genai is not installed.") from exc

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_id,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
    )
    text = getattr(response, "text", None)
    if text and str(text).strip():
        return str(text)
    parts = getattr(response, "parts", None)
    if not parts and getattr(response, "candidates", None):
        content = response.candidates[0].content
        parts = getattr(content, "parts", []) if content else []
    chunks: list[str] = []
    for part in parts or []:
        value = getattr(part, "text", None)
        if value:
            chunks.append(str(value))
    joined = "\n".join(chunks).strip()
    if not joined:
        raise RuntimeError("The translation model returned no text.")
    return joined
