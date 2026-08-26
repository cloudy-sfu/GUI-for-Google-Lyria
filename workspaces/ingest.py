"""Persist a generation result into the open project folder."""
import uuid
from pathlib import Path

from audio.io import load_bytes, save
from workspaces.models import (
    ContentPart,
    Conversation,
    GenerationParams,
    Message,
    OriginalMedia,
    Track,
    TrackSource,
    utc_now,
)
from workspaces.project import Project
from workspaces.transcript import Cue, cues_from_lyric_text
from llm.base import GeneratedAudio, GenerationRequest, GenerationResult, HistoryTurn

_AUDIO_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
}
_WAV_MIMES = {"audio/wav", "audio/x-wav", "audio/wave"}
_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def persist_generation(
    project: Project,
    provider,
    request: GenerationRequest,
    result: GenerationResult,
    image_paths: list[Path] | None = None,
    conversation_id: str | None = None,
    include_user_turn: bool = True,
) -> list[str]:
    """Write media, tracks, transcripts, and conversation messages. Returns new track ids."""
    conversation = (
        project.conversation_by_id(conversation_id) or project.ensure_active_conversation()
    )
    user_message: Message | None = None
    if include_user_turn:
        user_parts: list[ContentPart] = [ContentPart(type="text", text=request.prompt)]
        for index, path in enumerate(image_paths or []):
            media_id = f"img-{uuid.uuid4().hex}"
            dest = project.copy_media(path, media_id, "image")
            mime = (
                request.image_mimes[index]
                if index < len(request.image_mimes) and request.image_mimes[index]
                else _IMAGE_EXT_TO_MIME.get(dest.suffix.lower(), "image/png")
            )
            user_parts.append(ContentPart(type="image", media_id=media_id, mime=mime))

        user_message = Message(
            id=f"msg-{uuid.uuid4().hex}",
            role="user",
            timestamp=utc_now(),
            parts=user_parts,
            generation=GenerationParams(
                model=provider.model_id,
                negative_prompt=request.negative_prompt,
            ),
        )

    assistant_parts: list[ContentPart] = []
    if result.text:
        assistant_parts.append(ContentPart(type="text", text=result.text))

    track_ids: list[str] = []
    for audio in result.audios:
        uid = uuid.uuid4().hex
        media_id = f"aud-{uid}"
        dest, sample_rate, channels, duration_ms = _write_generated_audio(
            project, media_id, audio
        )
        track_id = f"trk-{uid}"
        track = Track(
            id=track_id,
            name=f"{provider.model_id} {uid[:8]}",
            source=TrackSource(type="lyria", model=provider.model_id),
            media_id=media_id,
            original=OriginalMedia(
                path=project.rel(dest),
                sample_rate=sample_rate,
                channels=channels,
                duration_ms=duration_ms,
            ),
        )
        cues = cues_from_lyric_text(result.text, duration_ms) if result.text else []
        if len(result.lyrics) > len(cues):
            cues = _clone_cues(result.lyrics, duration_ms)
        if cues:
            project.save_transcript(track, "en", "lyria_lyrics", cues)
        project.add_track(track)
        track_ids.append(track_id)
        assistant_parts.append(
            ContentPart(type="audio", track_id=track_id, media_id=media_id)
        )

    assistant_message = Message(
        id=f"msg-{uuid.uuid4().hex}",
        role="assistant",
        timestamp=utc_now(),
        parts=assistant_parts,
    )
    for track_id in track_ids:
        track = project.track_by_id(track_id)
        if track is not None:
            track.source.message_id = assistant_message.id

    if user_message is not None:
        conversation.messages.append(user_message)
    conversation.messages.append(assistant_message)
    conversation.model = provider.model_id
    conversation.auto_title_from_first_prompt()
    conversation.modified_at = utc_now()
    project.conversation_log.conversations.sort(
        key=lambda item: item.modified_at,
        reverse=True,
    )
    project.conversation_log.active_id = conversation.id
    project.mark_dirty()
    project.save()
    return track_ids


def _clone_cues(cues: list[Cue], duration_ms: int) -> list[Cue]:
    cloned = [Cue(start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text) for cue in cues]
    if cloned and duration_ms:
        cloned[-1].end_ms = max(cloned[-1].end_ms, duration_ms)
    return cloned


def _write_generated_audio(
    project: Project, media_id: str, audio: GeneratedAudio
) -> tuple[Path, int, int, int]:
    """Store generated audio as WAV. Returns dest, sample_rate, channels, duration_ms."""
    sample_rate = audio.sample_rate or project.sample_rate
    channels = audio.channels or 2
    duration_ms = 30_000
    clip = None
    try:
        clip = load_bytes(audio.data, audio.mime)
        sample_rate = clip.sample_rate
        channels = clip.channels
        duration_ms = round(clip.duration_ms)
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    if (audio.mime or "").lower() in _WAV_MIMES or audio.data[:4] == b"RIFF":
        dest = project.write_bytes(audio.data, media_id, ".wav", "audio")
    elif clip is not None:
        dest = project.media_audio / f"{media_id}.wav"
        save(clip, dest, fmt="wav")
    else:
        suffix = _AUDIO_EXT.get((audio.mime or "").lower(), ".mp3")
        dest = project.write_bytes(audio.data, media_id, suffix, "audio")
    return dest, sample_rate, channels, duration_ms


def generation_history(project: Project, conversation: Conversation) -> list[HistoryTurn]:
    """Build provider history from persisted conversation messages."""
    turns: list[HistoryTurn] = []
    for message in conversation.messages:
        if message.role != "user":
            turns.append(
                HistoryTurn(
                    role="assistant",
                    text=message.text().strip() or "[generated audio]",
                )
            )
            continue
        images: list[bytes] = []
        mimes: list[str] = []
        for part in message.parts:
            if part.type != "image" or not part.media_id:
                continue
            found = project.find_media(part.media_id)
            if found is None or not found.is_file():
                continue
            try:
                images.append(found.read_bytes())
            except OSError:
                continue
            mimes.append(part.mime or "image/png")
        turns.append(
            HistoryTurn(
                role="user",
                text=message.text().strip(),
                images=images,
                image_mimes=mimes,
                negative_prompt=(
                    message.generation.negative_prompt if message.generation else None
                ),
            )
        )
    return turns
