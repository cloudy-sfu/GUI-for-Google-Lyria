"""MusicProvider abstraction. UI and engine depend only on these types."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, NamedTuple


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
    seed: int | None = None
    sample_count: int = 1
    extra: dict[str, Any] = field(default_factory=dict)
    history: list[HistoryTurn] = field(default_factory=list)


@dataclass
class GeneratedAudio:
    data: bytes
    mime: str
    samplerate: int | None = None
    channels: int | None = None


@dataclass
class TimedLyric:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class GenerationResult:
    audios: list[GeneratedAudio]
    text: str | None = None
    lyrics: list[TimedLyric] = field(default_factory=list)
    raw: dict[str, Any] | None = None


class Capabilities(NamedTuple):
    accepts_text: bool
    accepts_images: bool
    returns_text: bool
    returns_lyrics: bool
    output_mimes: tuple[str, ...]


class MusicProvider(ABC):
    model_id: str
    display_name: str

    @abstractmethod
    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult:
        raise NotImplementedError
