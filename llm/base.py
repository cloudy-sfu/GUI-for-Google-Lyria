"""Provider-neutral request and result types. UI and engine depend only on these."""
from dataclasses import dataclass, field

from workspaces.transcript import Cue


@dataclass
class HistoryTurn:
    role: str
    text: str = ""
    images: list[bytes] = field(default_factory=list)
    image_mimes: list[str] = field(default_factory=list)
    negative_prompt: str | None = None


@dataclass
class GenerationRequest:
    prompt: str
    images: list[bytes] = field(default_factory=list)
    image_mimes: list[str] = field(default_factory=list)
    negative_prompt: str | None = None
    history: list[HistoryTurn] = field(default_factory=list)


@dataclass
class GeneratedAudio:
    data: bytes
    mime: str
    samplerate: int | None = None
    channels: int | None = None


@dataclass
class GenerationResult:
    audios: list[GeneratedAudio]
    text: str | None = None
    lyrics: list[Cue] = field(default_factory=list)
