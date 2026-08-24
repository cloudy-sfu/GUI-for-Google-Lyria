"""AppContext, Settings, and Registry. Configuration is injected, never read from module globals."""



import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


from audio.clip import AudioClip
from audio.operations import (
    channels_op,
    cut,
    fade_in,
    fade_out,
    reverse,
    speed,
    volume,
)
from llm.lyria3 import Lyria3Provider
from workspaces.models import DEFAULT_COMPOSITION_MODEL, resolve_model_id
from workspaces.project import Project

SCHEMA_VERSION = 1
APP_NAME = "GUI for Google Lyria"
DEFAULT_TRANSLATION_MODEL = "gemini-3.5-flash-lite"


def resolve_composition_model(value: str | None) -> str:
    return resolve_model_id(value) or DEFAULT_COMPOSITION_MODEL


class Registry:
    """Generic name-based factory registry. Shared by providers and effects."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._factories: dict[str, Callable] = {}

    def register(self, name: str, factory: Callable) -> None:
        if name in self._factories:
            raise ValueError(f"{self._kind} '{name}' already registered")
        self._factories[name] = factory

    def create(self, name: str, *args, **kwargs):
        try:
            return self._factories[name](*args, **kwargs)
        except KeyError as exc:
            raise KeyError(f"unknown {self._kind}: {name}") from exc

    def names(self) -> list[str]:
        return list(self._factories)

    def contains(self, name: str) -> bool:
        return name in self._factories


def default_settings_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME / "settings.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "gui-for-google-lyria" / "settings.json"


@dataclass
class Settings:
    composition_model: str = DEFAULT_COMPOSITION_MODEL
    gemini_api_key: str | None = None
    vertex_project: str | None = None
    vertex_location: str = "us-central1"
    theme: str = "system"
    samplerate: int = 48000
    default_channel_layout: str = "stereo"
    export_format: str = "wav"
    export_mp3_quality: str = "2"
    clip_protection: str = "headroom"
    translation_model: str = DEFAULT_TRANSLATION_MODEL
    recent_projects: list[str] = field(default_factory=list)
    settings_path: Path | None = None

    def resolved_gemini_api_key(self) -> str | None:
        env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if env:
            return env
        return self.gemini_api_key or None

    def remember_project(self, folder: Path) -> None:
        path = str(folder.resolve())
        recent = [p for p in self.recent_projects if p != path]
        self.recent_projects = [path, *recent][:12]

    @classmethod
    def from_dict(cls, data, path: Path | None = None) -> Settings:
        return cls(
            composition_model=resolve_composition_model(
                data.get("composition_model") or data.get("default_provider")
            ),
            gemini_api_key=data.get("gemini_api_key"),
            vertex_project=data.get("vertex_project"),
            vertex_location=data.get("vertex_location", "us-central1"),
            theme=data.get("theme", "system"),
            samplerate=int(data.get("samplerate", 48000)),
            default_channel_layout=data.get("default_channel_layout", "stereo"),
            export_format=data.get("export_format", "wav"),
            export_mp3_quality=str(data.get("export_mp3_quality", "2")),
            clip_protection=data.get("clip_protection", "headroom"),
            translation_model=str(
                data.get("translation_model") or DEFAULT_TRANSLATION_MODEL
            ).strip()
            or DEFAULT_TRANSLATION_MODEL,
            recent_projects=list(data.get("recent_projects") or []),
            settings_path=path,
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or default_settings_path()
        if not path.is_file():
            return cls(settings_path=path)
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.from_dict(data, path=path)

    def save(self) -> None:
        path = self.settings_path or default_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "composition_model": self.composition_model or DEFAULT_COMPOSITION_MODEL,
                    "gemini_api_key": self.gemini_api_key,
                    "theme": self.theme,
                    "samplerate": self.samplerate,
                    "default_channel_layout": self.default_channel_layout,
                    "export_format": self.export_format,
                    "export_mp3_quality": self.export_mp3_quality,
                    "clip_protection": self.clip_protection,
                    "translation_model": self.translation_model or DEFAULT_TRANSLATION_MODEL,
                    "recent_projects": list(self.recent_projects),
                },
                indent=4, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        self.settings_path = path


@dataclass
class AppContext:
    settings: Settings
    providers: Registry
    effects: Registry
    current_project: Project | None = None


def register_builtin_providers(registry: Registry, settings: Settings) -> None:
    settings.composition_model = resolve_composition_model(settings.composition_model)
    registry.register(
        "lyria-3-clip-preview",
        lambda: Lyria3Provider(settings, model_id="lyria-3-clip-preview"),
    )
    registry.register(
        "lyria-3-pro-preview",
        lambda: Lyria3Provider(settings, model_id="lyria-3-pro-preview"),
    )


class FunctionEffect:
    def __init__(self, name: str, func: Callable[..., AudioClip]) -> None:
        self.name = name
        self._func = func

    def apply(self, clip: AudioClip, params) -> AudioClip:
        return self._func(clip, **params)


def register_builtin_effects(registry: Registry) -> None:
    registry.register("cut", lambda: FunctionEffect("cut", cut))
    registry.register("fade_in", lambda: FunctionEffect("fade_in", fade_in))
    registry.register("fade_out", lambda: FunctionEffect("fade_out", fade_out))
    registry.register("volume", lambda: FunctionEffect("volume", volume))
    registry.register("speed", lambda: FunctionEffect("speed", speed))
    registry.register("reverse", lambda: FunctionEffect("reverse", reverse))
    registry.register("channels", lambda: FunctionEffect("channels", channels_op))


def build_app_context(settings: Settings | None = None) -> AppContext:
    settings = settings or Settings.load()
    providers = Registry("provider")
    effects = Registry("effect")
    register_builtin_providers(providers, settings)
    register_builtin_effects(effects)
    return AppContext(settings=settings, providers=providers, effects=effects)
