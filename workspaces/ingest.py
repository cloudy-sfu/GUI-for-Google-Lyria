"""Persist a generation result into the open project folder."""



import uuid
from pathlib import Path

import numpy as np

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
from llm.base import (
    GeneratedAudio,
    GenerationRequest,
    GenerationResult,
    HistoryTurn,
    MusicProvider,
)

_AUDIO_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
}
_WAV_MIMES = {"audio/wav", "audio/x-wav", "audio/wave"}
_IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def persist_generation(
    project: Project,
    provider: MusicProvider,
    request: GenerationRequest,
    result: GenerationResult,
    image_paths: list[Path] | None = None,
    conversation_id: str | None = None,
    include_user_turn: bool = True,
) -> list[str]:
    """Write media, tracks, transcripts, and conversation messages. Returns new track ids."""
    conversation = project.conversation_by_id(conversation_id)
    if conversation is None:
        conversation = project.ensure_active_conversation()
    user_message: Message | None = None
    if include_user_turn:
        user_parts: list[ContentPart] = [ContentPart(type="text", text=request.prompt)]
        image_paths = image_paths or []
        for index, path in enumerate(image_paths):
            media_id = f"img-{uuid.uuid4().hex}"
            dest = project.copy_media(path, media_id, "image")
            mime = _IMAGE_EXT_TO_MIME.get(dest.suffix.lower(), "image/png")
            if index < len(request.image_mimes) and request.image_mimes[index]:
                mime = request.image_mimes[index]
            user_parts.append(ContentPart(type="image", media_id=media_id, mime=mime))

        user_message = Message(
            id=f"msg-{uuid.uuid4().hex}",
            role="user",
            timestamp=utc_now(),
            parts=user_parts,
            generation=GenerationParams(
                model=provider.model_id,
                negative_prompt=request.negative_prompt,
                seed=request.seed,
                sample_count=request.sample_count,
                extra=dict(request.extra),
            ),
        )

    assistant_parts: list[ContentPart] = []
    if result.text:
        assistant_parts.append(ContentPart(type="text", text=result.text))

    track_ids: list[str] = []
    for index, audio in enumerate(result.audios):
        media_id = f"aud-{uuid.uuid4().hex}"
        dest, samplerate, channels, duration_ms = _write_generated_audio(
            project, media_id, audio
        )
        track_id = f"trk-{uuid.uuid4().hex}"
        name = f"{provider.display_name} {len(project.tracks) + 1}"
        if len(result.audios) > 1:
            name = f"{name} ({index + 1})"
        track = Track(
            id=track_id,
            name=name,
            source=TrackSource(type="lyria", model=provider.model_id, message_id=None),
            media_id=media_id,
            original=OriginalMedia(
                path=project.rel(dest),
                samplerate=samplerate,
                channels=channels,
                duration_ms=duration_ms,
            ),
        )
        cues: list[Cue] = []
        if result.text:
            cues = cues_from_lyric_text(result.text, duration_ms)
        if result.lyrics and len(result.lyrics) > len(cues):
            cues = [
                Cue(start_ms=item.start_ms, end_ms=item.end_ms, text=item.text)
                for item in result.lyrics
            ]
            if duration_ms and cues:
                cues[-1].end_ms = int(np.maximum(cues[-1].end_ms, duration_ms))
        if cues:
            project.save_transcript(track, "en", "lyria_lyrics", cues)
        project.add_track(track)
        track.source.message_id = None
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


def _is_wav_payload(audio: GeneratedAudio) -> bool:
    mime = (audio.mime or "").lower()
    return mime in _WAV_MIMES or audio.data[:4] == b"RIFF"


def _write_generated_audio(
    project: Project, media_id: str, audio: GeneratedAudio
) -> tuple[Path, int, int, int]:
    """Store generated audio as WAV. Returns dest, samplerate, channels, duration_ms."""
    samplerate = audio.samplerate or project.settings.samplerate
    channels = audio.channels or 2
    duration_ms = 30_000
    clip = None
    try:
        clip = load_bytes(audio.data, audio.mime)
        samplerate = clip.samplerate
        channels = clip.channels
        duration_ms = int(np.round(clip.duration_ms))
    except (ImportError, OSError, RuntimeError, ValueError):
        pass
    if _is_wav_payload(audio):
        dest = project.write_bytes(audio.data, media_id, ".wav", "audio")
        return dest, samplerate, channels, duration_ms
    if clip is not None:
        dest = project.media_audio / f"{media_id}.wav"
        save(clip, dest, fmt="wav")
        return dest, samplerate, channels, duration_ms
    suffix = _AUDIO_EXT.get((audio.mime or "").lower(), ".mp3")
    dest = project.write_bytes(audio.data, media_id, suffix, "audio")
    return dest, samplerate, channels, duration_ms


def generation_history(project: Project, conversation: Conversation) -> list[HistoryTurn]:
    """Build provider history from persisted conversation messages."""
    turns: list[HistoryTurn] = []
    for message in conversation.messages:
        if message.role == "user":
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
            negative = (
                message.generation.negative_prompt if message.generation is not None else None
            )
            turns.append(
                HistoryTurn(
                    role="user",
                    text=message.text().strip(),
                    images=images,
                    image_mimes=mimes,
                    negative_prompt=negative,
                )
            )
            continue
        turns.append(
            HistoryTurn(
                role="assistant",
                text=message.text().strip() or "[generated audio]",
            )
        )
    return turns


_IMAGE_EXT_TO_MIME = {ext: mime for mime, ext in _IMAGE_EXT.items()}
_IMAGE_EXT_TO_MIME[".jpg"] = "image/jpeg"
_IMAGE_EXT_TO_MIME[".jpeg"] = "image/jpeg"
