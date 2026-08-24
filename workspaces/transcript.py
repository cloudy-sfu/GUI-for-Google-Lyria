"""Timed transcripts: LRC on disk, with Lyria and WebVTT as import sources.

Lyria 3 returns a timed lyric dialect (`[16.0:]` plus `[[A0]]` section tags)
that converts cleanly to LRC, so the app stores, imports, exports, and
translates lyrics as LRC. Older `.vtt` files still load.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

_TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.](\d{1,3})"
)
_BRACKET = re.compile(
    r"\[(\d+):(\d{2})(?:\.(\d{1,3}))?\s*[-–]\s*(\d+):(\d{2})(?:\.(\d{1,3}))?\]\s*(.*)"
)
# Standard LRC: [mm:ss.xx] or [hh:mm:ss.xx]; minutes may exceed 59.
_LRC_TAG = re.compile(
    r"\[(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]"
)
_LRC_OFFSET = re.compile(r"^\[offset:([+-]?\d+)\]\s*$", re.IGNORECASE)
_LRC_META = re.compile(
    r"^\[(ti|ar|al|au|by|offset|length|re|ve|la):",
    re.IGNORECASE,
)
# Lyria 3 text: [[A0]] section markers, [16.0:] timed lines, [:] continuations.
_LYRIA_TIME = re.compile(r"\[\s*\d+(?:\.\d+)?\s*\:\s*\]")
_LYRIA_MARK = re.compile(
    r"\[\[([A-Za-z]+\d+)\]\]|"
    r"\[\s*(\d+(?:\.\d+)?)\s*\:\s*\]|"
    r"\[\s*\:\s*\]"
)
_DEFAULT_LINE_MS = 4_000


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Transcript:
    track_id: str
    language: str
    source: str
    cues: list[Cue] = field(default_factory=list)


def decode_text(data: bytes) -> str:
    """Decode a lyric file, trying the encodings editors actually produce."""
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _hms_to_ms(hours: str | None, minutes: str, seconds: str, millis: str) -> int:
    h = int(hours or 0)
    whole_millis = millis.ljust(3, "0")[:3]
    return ((h * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(whole_millis)


def parse_vtt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    if lines and lines[0].strip().upper().startswith("WEBVTT"):
        i = 1
    while i < len(lines):
        line = lines[i].strip()
        match = _TS.search(line)
        if not match:
            i += 1
            continue
        start = _hms_to_ms(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _hms_to_ms(match.group(5), match.group(6), match.group(7), match.group(8))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i].rstrip())
            i += 1
        cues.append(Cue(start_ms=start, end_ms=end, text="\n".join(body).strip()))
    return cues


def _frac_to_ms(frac: str | None) -> int:
    if not frac:
        return 0
    if len(frac) == 1:
        return int(frac) * 100
    if len(frac) == 2:
        return int(frac) * 10
    return int(frac[:3].ljust(3, "0")[:3])


def _lrc_tag_to_ms(match: re.Match[str]) -> int:
    hours = match.group(1)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    total_minutes = (int(hours) * 60 + minutes) if hours is not None else minutes
    return (total_minutes * 60 + seconds) * 1000 + _frac_to_ms(match.group(4))


def ms_to_lrc(ms: int) -> str:
    """Standard LRC timestamp `[mm:ss.xx]` using total minutes and centiseconds."""
    minutes, rem = divmod(max(0, int(ms)), 60_000)
    seconds, millis = divmod(rem, 1000)
    cs = round(millis / 10.0)
    if cs >= 100:
        seconds += cs // 100
        cs %= 100
        minutes += seconds // 60
        seconds %= 60
    return f"[{minutes:02d}:{seconds:02d}.{cs:02d}]"


def _assign_end_times(cues: list[Cue], duration_ms: int | None) -> None:
    for i, cue in enumerate(cues):
        if i + 1 < len(cues):
            cue.end_ms = max(cues[i + 1].start_ms, cue.start_ms + 1)
        elif duration_ms is not None and duration_ms > cue.start_ms:
            cue.end_ms = duration_ms
        else:
            cue.end_ms = max(cue.end_ms, cue.start_ms + _DEFAULT_LINE_MS)


def parse_lrc(text: str, duration_ms: int | None = None) -> list[Cue]:
    """Parse LRC by content. Id tags are ignored except `[offset:]`."""
    offset_ms = 0
    timed: list[tuple[int, str]] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        offset_match = _LRC_OFFSET.match(line)
        if offset_match:
            offset_ms = int(offset_match.group(1))
            continue
        if _LRC_META.match(line) and not _LRC_TAG.search(line):
            continue
        tags = list(_LRC_TAG.finditer(line))
        if not tags:
            continue
        lyric = line[tags[-1].end() :].strip()
        for tag in tags:
            timed.append((max(0, _lrc_tag_to_ms(tag) + offset_ms), lyric))
    if not timed:
        return []
    timed.sort(key=lambda item: item[0])
    cues = [
        Cue(start_ms=start, end_ms=start + _DEFAULT_LINE_MS, text=body or " ")
        for start, body in timed
    ]
    _assign_end_times(cues, duration_ms)
    return cues


def dump_lrc(
    cues: list[Cue],
    *,
    title: str | None = None,
    language: str | None = None,
) -> str:
    lines: list[str] = []
    if title:
        lines.append(f"[ti:{title}]")
    if language:
        lines.append(f"[la:{language}]")
    if lines:
        lines.append("")
    for cue in cues:
        body = " ".join(cue.text.split())
        lines.append(f"{ms_to_lrc(cue.start_ms)}{body}")
    return "\n".join(lines).rstrip() + "\n"


def write_transcript_file(path: Path, cues: list[Cue], **meta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lrc(cues, **meta), encoding="utf-8")


def parse_imported_lrc(text: str, duration_ms: int | None = None) -> list[Cue]:
    """Import path: recognize LRC by timestamps, ignoring the file extension."""
    return parse_lrc(text.lstrip("\ufeff"), duration_ms)


def read_transcript_file(path: Path, duration_ms: int | None = None) -> list[Cue]:
    """Load whatever timed format is on disk: LRC, WebVTT, or Lyria text."""
    text = decode_text(path.read_bytes()).lstrip("\ufeff")
    if text.lstrip()[:16].upper().startswith("WEBVTT"):
        return parse_vtt(text)
    return (
        parse_lrc(text, duration_ms)
        or _parse_lyria_lyric_cues(text, duration_ms)
        or parse_vtt(text)
    )


def cues_from_lyric_text(text: str, duration_ms: int | None = None) -> list[Cue]:
    """Best-effort conversion of Lyria 3 lyric/structure text into cues."""
    parsed = _parse_lyria_lyric_cues(text, duration_ms) or parse_lrc(text, duration_ms)
    if parsed:
        return parsed
    cues: list[Cue] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _BRACKET.match(line)
        if match:
            start = (int(match.group(1)) * 60 + int(match.group(2))) * 1000
            if match.group(3):
                start += int(match.group(3).ljust(3, "0")[:3])
            end = (int(match.group(4)) * 60 + int(match.group(5))) * 1000
            if match.group(6):
                end += int(match.group(6).ljust(3, "0")[:3])
            body = (match.group(7) or "").strip()
            cues.append(Cue(start_ms=start, end_ms=max(end, start + 1), text=body or line))
            continue
        vtt_match = _TS.search(line)
        if vtt_match:
            start = _hms_to_ms(
                vtt_match.group(1), vtt_match.group(2), vtt_match.group(3), vtt_match.group(4)
            )
            end = _hms_to_ms(
                vtt_match.group(5), vtt_match.group(6), vtt_match.group(7), vtt_match.group(8)
            )
            rest = line[vtt_match.end() :].strip(" -:|")
            cues.append(Cue(start_ms=start, end_ms=end, text=rest or line))
    if cues:
        _extend_last_cue(cues, duration_ms)
        return cues
    stripped = text.strip()
    if not stripped:
        return []
    end = duration_ms if duration_ms and duration_ms > 0 else 30_000
    return [Cue(start_ms=0, end_ms=end, text=stripped)]


def expand_unparsed_lyria_cues(
    cues: list[Cue], duration_ms: int | None = None
) -> list[Cue]:
    """Re-parse a dumped Lyria lyric blob that was stored as a single cue."""
    if not cues or not any(_LYRIA_TIME.search(cue.text) for cue in cues):
        return cues
    blob = "\n".join(cue.text for cue in cues)
    return _parse_lyria_lyric_cues(blob, duration_ms) or cues


def _clean_lyric(text: str) -> str:
    return " ".join(text.split()).strip()


def _tokenize_lyria(text: str) -> list[tuple[str, str | int | None]]:
    tokens: list[tuple[str, str | int | None]] = []
    pos = 0
    for match in _LYRIA_MARK.finditer(text):
        if match.start() > pos and text[pos : match.start()].strip():
            tokens.append(("text", text[pos : match.start()]))
        if match.group(1) is not None:
            tokens.append(("section", match.group(0)))
        elif match.group(2) is not None:
            tokens.append(("time", round(float(match.group(2)) * 1000)))
        else:
            tokens.append(("cont", None))
        pos = match.end()
    if pos < len(text) and text[pos:].strip():
        tokens.append(("text", text[pos:]))
    return tokens


def _take_following_text(
    tokens: list[tuple[str, str | int | None]], index: int
) -> tuple[str, int]:
    nxt = index + 1
    if nxt < len(tokens) and tokens[nxt][0] == "text":
        return _clean_lyric(str(tokens[nxt][1])), nxt + 1
    return "", index + 1


def _spread_times(starts: list[int | None], duration_ms: int | None) -> list[int]:
    n = len(starts)
    out: list[int | None] = list(starts)
    if not out:
        return []
    if out[0] is None:
        out[0] = 0
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        prev = i - 1
        nxt = i
        while nxt < n and out[nxt] is None:
            nxt += 1
        prev_t = int(out[prev]) if prev >= 0 and out[prev] is not None else 0
        if nxt < n and out[nxt] is not None:
            next_t = int(out[nxt])
        elif duration_ms is not None and duration_ms > prev_t:
            next_t = duration_ms
        else:
            next_t = prev_t + _DEFAULT_LINE_MS * (nxt - prev)
        span = max(1, next_t - prev_t)
        steps = max(1, nxt - prev)
        for j in range(prev + 1, nxt):
            out[j] = prev_t + span * (j - prev) // steps
        i = nxt
    return [int(value or 0) for value in out]


def _extend_last_cue(cues: list[Cue], duration_ms: int | None) -> None:
    if not cues or duration_ms is None or duration_ms <= 0:
        return
    cues[-1].end_ms = max(cues[-1].end_ms, duration_ms)


def _parse_lyria_lyric_cues(text: str, duration_ms: int | None) -> list[Cue]:
    if not _LYRIA_TIME.search(text):
        return []
    tokens = _tokenize_lyria(text)
    lines: list[tuple[int | None, str]] = []
    pending_tags: list[str] = []
    pending_start: int | None = None

    def prefixed(body: str) -> str:
        nonlocal pending_tags
        if not pending_tags:
            return body
        tagged = " ".join([*pending_tags, body] if body else pending_tags)
        pending_tags = []
        return tagged

    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "section":
            pending_tags.append(str(value))
            index += 1
            continue
        if kind == "time":
            pending_start = int(value) if value is not None else 0
            body, index = _take_following_text(tokens, index)
            if body or pending_tags:
                lines.append((pending_start, prefixed(body)))
                pending_start = None
            continue
        if kind == "cont":
            body, index = _take_following_text(tokens, index)
            start = pending_start
            pending_start = None
            text_value = prefixed(body)
            if text_value:
                lines.append((start, text_value))
            continue
        if kind == "text":
            body = _clean_lyric(str(value))
            if lines and body:
                start, existing = lines[-1]
                lines[-1] = (start, f"{existing} {body}".strip())
            index += 1
            continue
        index += 1

    if pending_tags:
        start = pending_start
        if start is None and lines:
            start = lines[-1][0]
        lines.append((start if start is not None else 0, " ".join(pending_tags)))

    if not lines:
        return []

    times = _spread_times([start for start, _text in lines], duration_ms)
    cues: list[Cue] = []
    for i, (_start, body) in enumerate(lines):
        start = times[i]
        if i + 1 < len(times):
            end = times[i + 1]
        elif duration_ms is not None and duration_ms > start:
            end = duration_ms
        else:
            end = start + _DEFAULT_LINE_MS
        cues.append(Cue(start_ms=start, end_ms=max(end, start + 1), text=body or " "))
    return cues
