"""Project, conversation, track, and mix dataclasses."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


Role = Literal["user", "assistant"]
DEFAULT_CONVERSATION_TITLE = "New conversation"
CONVERSATION_LOG_VERSION = 2
DEFAULT_COMPOSITION_MODEL = "lyria-3-pro-preview"
_MODEL_ALIASES = {
    "lyria-3-pro": "lyria-3-pro-preview",
    "lyria-3-clip": "lyria-3-clip-preview",
    "lyria-2": "lyria-3-pro-preview",
}


def resolve_model_id(value: str | None) -> str:
    """Map legacy short ids to Gemini model ids. Unknown values pass through."""
    text = (value or "").strip()
    if not text:
        return ""
    return _MODEL_ALIASES.get(text, text)


def title_from_prompt(text: str, fallback: str = DEFAULT_CONVERSATION_TITLE) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return fallback
    if len(line) > 48:
        return line[:45].rstrip() + "…"
    return line


@dataclass
class GenerationParams:
    model: str
    negative_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"model": self.model}
        if self.negative_prompt:
            data["negative_prompt"] = self.negative_prompt
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GenerationParams | None:
        if not data:
            return None
        return cls(
            model=resolve_model_id(data.get("model")),
            negative_prompt=data.get("negative_prompt"),
        )


@dataclass
class ContentPart:
    type: str
    text: str | None = None
    media_id: str | None = None
    mime: str | None = None
    track_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            data["text"] = self.text
        if self.media_id is not None:
            data["media_id"] = self.media_id
        if self.mime is not None:
            data["mime"] = self.mime
        if self.track_id is not None:
            data["track_id"] = self.track_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContentPart:
        return cls(
            type=data["type"],
            text=data.get("text"),
            media_id=data.get("media_id"),
            mime=data.get("mime"),
            track_id=data.get("track_id"),
        )


@dataclass
class Message:
    id: str
    role: Role
    timestamp: str
    parts: list[ContentPart] = field(default_factory=list)
    generation: GenerationParams | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "timestamp": self.timestamp,
            "parts": [part.to_dict() for part in self.parts],
        }
        if self.generation is not None:
            data["generation"] = self.generation.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            id=data["id"],
            role=data["role"],
            timestamp=data["timestamp"],
            parts=[ContentPart.from_dict(part) for part in data.get("parts") or []],
            generation=GenerationParams.from_dict(data.get("generation")),
        )

    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if part.type == "text" and part.text)


@dataclass
class Conversation:
    id: str
    title: str = DEFAULT_CONVERSATION_TITLE
    created_at: str = field(default_factory=utc_now)
    modified_at: str = field(default_factory=utc_now)
    model: str = ""
    messages: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "model": self.model,
            "messages": [message.to_dict() for message in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Conversation:
        messages = [Message.from_dict(item) for item in data.get("messages") or []]
        title = data.get("title") or title_from_prompt(
            next((message.text() for message in messages if message.role == "user"), "")
        )
        return cls(
            id=data.get("id") or f"convo-{uuid.uuid4().hex}",
            title=title,
            created_at=data.get("created_at") or utc_now(),
            modified_at=data.get("modified_at") or data.get("created_at") or utc_now(),
            model=resolve_model_id(str(data.get("model") or "")),
            messages=messages,
        )

    def resolved_model(self) -> str:
        if self.model.strip():
            return self.model.strip()
        for message in reversed(self.messages):
            generation = message.generation
            if generation is None:
                continue
            if generation.model and generation.model.strip():
                return generation.model.strip()
        return ""

    def display_title(self) -> str:
        return self.title.strip() or DEFAULT_CONVERSATION_TITLE

    def auto_title_from_first_prompt(self) -> None:
        if self.title and self.title != DEFAULT_CONVERSATION_TITLE:
            return
        for message in self.messages:
            if message.role != "user":
                continue
            text = message.text().strip()
            if text:
                self.title = title_from_prompt(text)
                return


@dataclass
class ConversationLog:
    schema_version: int = CONVERSATION_LOG_VERSION
    conversations: list[Conversation] = field(default_factory=list)
    active_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ConversationLog:
        data = data or {}
        if "conversations" in data:
            conversations = [
                Conversation.from_dict(item) for item in data.get("conversations") or []
            ]
            active_id = data.get("active_id")
            if active_id and not any(item.id == active_id for item in conversations):
                active_id = None
            if active_id is None and conversations:
                active_id = conversations[0].id
            return cls(
                schema_version=int(data.get("schema_version", CONVERSATION_LOG_VERSION)),
                conversations=conversations,
                active_id=active_id,
            )
        conversation = Conversation.from_dict(data)
        return cls(
            schema_version=CONVERSATION_LOG_VERSION,
            conversations=[conversation],
            active_id=conversation.id,
        )

    def by_id(self, conversation_id: str | None) -> Conversation | None:
        if not conversation_id:
            return None
        for item in self.conversations:
            if item.id == conversation_id:
                return item
        return None


@dataclass
class TrackSource:
    type: str
    model: str | None = None
    message_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.model:
            data["model"] = self.model
        if self.message_id:
            data["message_id"] = self.message_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackSource:
        return cls(
            type=data.get("type", "imported"),
            model=resolve_model_id(data.get("model")) or None,
            message_id=data.get("message_id"),
        )


@dataclass
class OriginalMedia:
    path: str
    samplerate: int
    channels: int
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "samplerate": self.samplerate,
            "channels": self.channels,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OriginalMedia:
        return cls(
            path=data["path"],
            samplerate=int(data["samplerate"]),
            channels=int(data["channels"]),
            duration_ms=int(data["duration_ms"]),
        )


@dataclass
class TranscriptRef:
    language: str
    path: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "path": self.path,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptRef:
        return cls(
            language=data["language"],
            path=data["path"],
            source=data.get("source", "manual"),
        )


@dataclass
class Track:
    id: str
    name: str
    source: TrackSource
    media_id: str
    original: OriginalMedia
    operations: list[dict[str, Any]] = field(default_factory=list)
    transcripts: list[TranscriptRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source.to_dict(),
            "media_id": self.media_id,
            "original": self.original.to_dict(),
            "operations": [dict(op) for op in self.operations],
            "transcripts": [item.to_dict() for item in self.transcripts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        return cls(
            id=data["id"],
            name=data["name"],
            source=TrackSource.from_dict(data.get("source") or {"type": "imported"}),
            media_id=data["media_id"],
            original=OriginalMedia.from_dict(data["original"]),
            operations=[dict(op) for op in data.get("operations") or []],
            transcripts=[
                TranscriptRef.from_dict(item) for item in data.get("transcripts") or []
            ],
        )

    def format_name(self) -> str:
        suffix = Path(self.original.path).suffix.lstrip(".").upper()
        return "OGG" if suffix == "OGA" else suffix

    def source_name(self) -> str:
        return self.source.type + (f"/{self.source.model}" if self.source.model else "")

    def labeled_name(self) -> str:
        """Two-line gutter label: name, then format and source."""
        suffix = self.format_name()
        if not suffix:
            return self.name
        return f"{self.name}\n{suffix}, {self.source_name()}"


@dataclass
class MixClip:
    track_id: str
    offset_ms: int = 0
    gain_db: float = 0.0
    mute: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "offset_ms": self.offset_ms,
            "gain_db": self.gain_db,
            "mute": self.mute,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MixClip:
        return cls(
            track_id=data["track_id"],
            offset_ms=int(data.get("offset_ms", 0)),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
        )


@dataclass
class Mix:
    name: str = "Main mix"
    channel_layout: str = "stereo"
    clips: list[MixClip] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "channel_layout": self.channel_layout,
            "clips": [clip.to_dict() for clip in self.clips],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Mix:
        if not data:
            return cls()
        return cls(
            name=data.get("name", "Main mix"),
            channel_layout=data.get("channel_layout", "stereo"),
            clips=[MixClip.from_dict(item) for item in data.get("clips") or []],
        )

    def clip_for_track(self, track_id: str) -> MixClip | None:
        for clip in self.clips:
            if clip.track_id == track_id:
                return clip
        return None


@dataclass
class ProjectSettings:
    samplerate: int = 48000
    default_channel_layout: str = "stereo"
    export_format: str = "wav"

    def to_dict(self) -> dict[str, Any]:
        return {
            "samplerate": self.samplerate,
            "default_channel_layout": self.default_channel_layout,
            "export_format": self.export_format,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProjectSettings:
        data = data or {}
        return cls(
            samplerate=int(data.get("samplerate", 48000)),
            default_channel_layout=data.get("default_channel_layout", "stereo"),
            export_format=data.get("export_format", "wav"),
        )
