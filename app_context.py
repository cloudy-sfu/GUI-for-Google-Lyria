"""AppContext and Settings. Configuration is injected, never read from module globals."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from workspaces.models import DEFAULT_COMPOSITION_MODEL, resolve_model_id
from workspaces.project import Project

SCHEMA_VERSION = 1
APP_NAME = "GUI for Google Lyria"
DEFAULT_TRANSLATION_MODEL = "gemini-3.5-flash-lite"


def resolve_composition_model(value: str | None) -> str:
    return resolve_model_id(value) or DEFAULT_COMPOSITION_MODEL


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
    def load(cls, path: Path | None = None) -> Settings:
        path = path or default_settings_path()
        if not path.is_file():
            return cls(settings_path=path)
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(
            composition_model=resolve_composition_model(data.get("composition_model")),
            gemini_api_key=data.get("gemini_api_key"),
            samplerate=int(data.get("samplerate", 48000)),
            default_channel_layout=data.get("default_channel_layout", "stereo"),
            export_format=data.get("export_format", "wav"),
            export_mp3_quality=str(data.get("export_mp3_quality", "2")),
            clip_protection=data.get("clip_protection", "headroom"),
            translation_model=str(data.get("translation_model") or "").strip()
            or DEFAULT_TRANSLATION_MODEL,
            recent_projects=list(data.get("recent_projects") or []),
            settings_path=path,
        )

    def save(self) -> None:
        path = self.settings_path or default_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "composition_model": self.composition_model or DEFAULT_COMPOSITION_MODEL,
                    "gemini_api_key": self.gemini_api_key,
                    "samplerate": self.samplerate,
                    "default_channel_layout": self.default_channel_layout,
                    "export_format": self.export_format,
                    "export_mp3_quality": self.export_mp3_quality,
                    "clip_protection": self.clip_protection,
                    "translation_model": self.translation_model or DEFAULT_TRANSLATION_MODEL,
                    "recent_projects": list(self.recent_projects),
                },
                indent=4,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
        self.settings_path = path


@dataclass
class AppContext:
    settings: Settings
    current_project: Project | None = None


def build_app_context() -> AppContext:
    settings = Settings.load()
    settings.composition_model = resolve_composition_model(settings.composition_model)
    return AppContext(settings=settings)
