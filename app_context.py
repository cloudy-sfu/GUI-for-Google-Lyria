"""AppContext and Settings. Configuration is injected, never read from module globals."""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from workspaces.project import Project

APP_NAME = "GUI for Google Lyria"

def default_settings_path() -> Path:
    if getattr(sys, "frozen", False):
        program_dir = Path(sys.executable).resolve().parent
    else:
        program_dir = Path(__file__).resolve().parent
    return program_dir / "settings.json"


@dataclass
class Settings:
    composition_model: str = ""
    gemini_api_key: str | None = None
    export_format: str = "wav"
    translation_model: str = ""
    recent_projects: list[str] = field(default_factory=list)
    settings_path: Path | None = None

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
        return cls(settings_path=path, **data)

    def save(self) -> None:
        path = self.settings_path or default_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "composition_model": self.composition_model,
                    "gemini_api_key": self.gemini_api_key,
                    "export_format": self.export_format,
                    "translation_model": self.translation_model,
                    "recent_projects": self.recent_projects,
                },
                indent=4,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.settings_path = path


@dataclass
class AppContext:
    settings: Settings
    current_project: Project | None = None
