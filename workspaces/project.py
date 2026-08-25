"""Load and save a folder-based project."""
import copy
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from workspaces.models import (
    Conversation,
    ConversationLog,
    DEFAULT_CONVERSATION_TITLE,
    Mix,
    MixClip,
    ProjectSettings,
    Track,
    TranscriptRef,
    utc_now,
)
from workspaces.transcript import (
    Cue,
    Transcript,
    expand_unparsed_lyria_cues,
    read_transcript_file,
    write_transcript_file,
)

SCHEMA_VERSION = 1


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object.")
    return data


@dataclass
class UndoStack:
    _past: list[tuple[list[Track], Mix]] = field(default_factory=list)
    _future: list[tuple[list[Track], Mix]] = field(default_factory=list)

    def can_undo(self) -> bool:
        return bool(self._past)

    def can_redo(self) -> bool:
        return bool(self._future)

    def push(self, tracks: list[Track], mix: Mix) -> None:
        self._past.append((copy.deepcopy(tracks), copy.deepcopy(mix)))
        self._future.clear()

    def undo(self, tracks: list[Track], mix: Mix) -> tuple[list[Track], Mix] | None:
        if not self._past:
            return None
        self._future.append((copy.deepcopy(tracks), copy.deepcopy(mix)))
        return self._past.pop()

    def redo(self, tracks: list[Track], mix: Mix) -> tuple[list[Track], Mix] | None:
        if not self._future:
            return None
        self._past.append((copy.deepcopy(tracks), copy.deepcopy(mix)))
        return self._future.pop()


@dataclass
class Project:
    root: Path
    created_at: str
    modified_at: str
    default_model: str
    settings: ProjectSettings
    conversation_log: ConversationLog = field(default_factory=ConversationLog)
    tracks: list[Track] = field(default_factory=list)
    mix: Mix = field(default_factory=Mix)
    dirty: bool = False
    undo_stack: UndoStack = field(default_factory=UndoStack)

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def media_audio(self) -> Path:
        return self.root / "media" / "audio"

    @property
    def media_images(self) -> Path:
        return self.root / "media" / "images"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "transcripts"

    def ensure_dirs(self) -> None:
        self.media_audio.mkdir(parents=True, exist_ok=True)
        self.media_images.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        return (self.root / relative).resolve()

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def track_by_id(self, track_id: str) -> Track | None:
        for track in self.tracks:
            if track.id == track_id:
                return track
        return None

    def find_media(self, media_id: str) -> Path | None:
        if not media_id:
            return None
        for folder in (self.media_audio, self.media_images):
            if not folder.is_dir():
                continue
            for path in folder.glob(f"{media_id}.*"):
                return path
        return None

    def conversation_by_id(self, conversation_id: str | None) -> Conversation | None:
        return self.conversation_log.by_id(conversation_id)

    def ensure_active_conversation(self) -> Conversation:
        found = self.conversation_by_id(self.conversation_log.active_id)
        if found is not None:
            return found
        if self.conversation_log.conversations:
            active = self.conversation_log.conversations[0]
            self.conversation_log.active_id = active.id
            return active
        return self.new_conversation()

    def set_active_conversation(self, conversation_id: str) -> None:
        found = self.conversation_by_id(conversation_id)
        if found is not None:
            self.conversation_log.active_id = found.id

    def new_conversation(
        self,
        title: str = DEFAULT_CONVERSATION_TITLE,
        model: str = "",
    ) -> Conversation:
        conversation = Conversation(
            id=f"convo-{uuid.uuid4().hex}",
            title=title,
            model=model,
        )
        self.conversation_log.conversations.insert(0, conversation)
        self.conversation_log.active_id = conversation.id
        self.mark_dirty()
        return conversation

    def rename_conversation(self, conversation_id: str, title: str) -> bool:
        found = self.conversation_by_id(conversation_id)
        if found is None:
            return False
        cleaned = title.strip() or DEFAULT_CONVERSATION_TITLE
        if found.title == cleaned:
            return False
        found.title = cleaned
        found.modified_at = utc_now()
        self.mark_dirty()
        return True

    def duplicate_conversation(self, conversation_id: str) -> Conversation | None:
        found = self.conversation_by_id(conversation_id)
        if found is None:
            return None
        clone = copy.deepcopy(found)
        clone.id = f"convo-{uuid.uuid4().hex}"
        now = utc_now()
        clone.created_at = now
        clone.modified_at = now
        clone.title = f"{found.display_title()} (copy)"
        for message in clone.messages:
            message.id = f"msg-{uuid.uuid4().hex}"
        self.conversation_log.conversations.insert(0, clone)
        self.conversation_log.active_id = clone.id
        self.mark_dirty()
        return clone

    def truncate_messages_from(self, conversation_id: str, message_id: str) -> bool:
        found = self.conversation_by_id(conversation_id)
        if found is None:
            return False
        index = next(
            (i for i, message in enumerate(found.messages) if message.id == message_id),
            None,
        )
        if index is None:
            return False
        found.messages = found.messages[:index]
        found.modified_at = utc_now()
        self.mark_dirty()
        return True

    def clear_conversation(self, conversation_id: str) -> bool:
        found = self.conversation_by_id(conversation_id)
        if found is None:
            return False
        found.messages.clear()
        found.modified_at = utc_now()
        self.mark_dirty()
        return True

    def delete_conversation(
        self,
        conversation_id: str,
        replacement_model: str = "",
    ) -> bool:
        conversations = self.conversation_log.conversations
        index = next(
            (i for i, item in enumerate(conversations) if item.id == conversation_id),
            None,
        )
        if index is None:
            return False
        conversations.pop(index)
        if not conversations:
            self.new_conversation(model=replacement_model)
        elif self.conversation_log.active_id == conversation_id:
            next_index = min(index, len(conversations) - 1)
            self.conversation_log.active_id = conversations[next_index].id
        self.mark_dirty()
        return True

    def mark_dirty(self) -> None:
        self.dirty = True
        self.modified_at = utc_now()

    def snapshot_edits(self) -> None:
        self.undo_stack.push(self.tracks, self.mix)

    def undo(self) -> bool:
        restored = self.undo_stack.undo(self.tracks, self.mix)
        if restored is None:
            return False
        self.tracks, self.mix = restored
        self.mark_dirty()
        return True

    def redo(self) -> bool:
        restored = self.undo_stack.redo(self.tracks, self.mix)
        if restored is None:
            return False
        self.tracks, self.mix = restored
        self.mark_dirty()
        return True

    def add_track(self, track: Track, offset_ms: int | None = None) -> None:
        self.tracks.append(track)
        if self.mix.clip_for_track(track.id) is None:
            if offset_ms is None:
                offset_ms = 0
            self.mix.clips.append(MixClip(track_id=track.id, offset_ms=offset_ms))
        self.mark_dirty()

    def remove_track(self, track_id: str) -> None:
        self.tracks = [track for track in self.tracks if track.id != track_id]
        self.mix.clips = [clip for clip in self.mix.clips if clip.track_id != track_id]
        self.mark_dirty()

    def move_track(self, track_id: str, index: int) -> bool:
        current = next((i for i, item in enumerate(self.tracks) if item.id == track_id), None)
        if current is None:
            return False
        target = max(0, min(int(index), len(self.tracks) - 1))
        if target == current:
            return False
        self.tracks.insert(target, self.tracks.pop(current))
        self.mark_dirty()
        return True

    def load_transcript(self, track: Track, language: str) -> Transcript | None:
        ref = next((item for item in track.transcripts if item.language == language), None)
        if ref is None:
            return None
        path = self.resolve(ref.path)
        if not path.is_file():
            return Transcript(track_id=track.id, language=language, source=ref.source)
        duration_ms = track.original.duration_ms if track.original else None
        original = read_transcript_file(path, duration_ms)
        cues = expand_unparsed_lyria_cues(original, duration_ms)
        if cues != original:
            try:
                write_transcript_file(path, cues, title=track.name, language=language)
            except OSError:
                pass
        return Transcript(
            track_id=track.id,
            language=language,
            source=ref.source,
            cues=cues,
        )

    def save_transcript(self, track: Track, language: str, source: str, cues: list[Cue]) -> None:
        relative = f"transcripts/{track.id}.{language}.lrc"
        path = self.root / relative
        write_transcript_file(path, cues, title=track.name, language=language)
        existing = next((item for item in track.transcripts if item.language == language), None)
        if existing is None:
            track.transcripts.append(
                TranscriptRef(language=language, path=relative, source=source)
            )
        else:
            old_path = self.root / existing.path
            existing.path = relative
            existing.source = source
            if old_path != path and old_path.is_file():
                try:
                    old_path.unlink()
                except OSError:
                    pass
        self.mark_dirty()

    def copy_media(self, source: Path, media_id: str, kind: str) -> Path:
        suffix = source.suffix.lower() or (".bin")
        folder = self.media_audio if kind == "audio" else self.media_images
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{media_id}{suffix}"
        shutil.copy2(source, dest)
        return dest

    def write_bytes(self, data: bytes, media_id: str, suffix: str, kind: str) -> Path:
        folder = self.media_audio if kind == "audio" else self.media_images
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{media_id}{suffix}"
        dest.write_bytes(data)
        return dest

    def save(self) -> None:
        self.ensure_dirs()
        self.modified_at = utc_now()
        _atomic_write_json(
            self.root / "project.json",
            {
                "schema_version": SCHEMA_VERSION,
                "created_at": self.created_at,
                "modified_at": self.modified_at,
                "default_model": self.default_model,
                "settings": self.settings.to_dict(),
            },
        )
        _atomic_write_json(
            self.root / "conversation.json",
            {
                "schema_version": self.conversation_log.schema_version,
                "active_id": self.conversation_log.active_id,
                "conversations": [
                    item.to_dict() for item in self.conversation_log.conversations
                ],
            },
        )
        _atomic_write_json(
            self.root / "tracks.json",
            {
                "schema_version": SCHEMA_VERSION,
                "tracks": [track.to_dict() for track in self.tracks],
                "mix": self.mix.to_dict(),
            },
        )
        self.dirty = False

    @classmethod
    def create(
        cls,
        root: Path,
        default_model: str,
        settings: ProjectSettings | None = None,
    ) -> Project:
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        now = utc_now()
        project = cls(
            root=root,
            created_at=now,
            modified_at=now,
            default_model=default_model,
            settings=settings or ProjectSettings(),
        )
        project.new_conversation(model=default_model)
        project.ensure_dirs()
        project.save()
        return project

    @classmethod
    def load(cls, root: Path) -> Project:
        root = root.resolve()
        project_path = root / "project.json"
        if not project_path.is_file():
            raise FileNotFoundError(f"Not a project folder (missing project.json): {root}")
        manifest = _read_json(project_path)
        conversation_data = (
            _read_json(root / "conversation.json")
            if (root / "conversation.json").is_file()
            else {"schema_version": 1, "messages": []}
        )
        tracks_data = (
            _read_json(root / "tracks.json")
            if (root / "tracks.json").is_file()
            else {"schema_version": 1, "tracks": [], "mix": {}}
        )
        project = cls(
            root=root,
            created_at=manifest.get("created_at") or utc_now(),
            modified_at=manifest.get("modified_at") or utc_now(),
            default_model=str(manifest.get("default_model") or "").strip(),
            settings=ProjectSettings.from_dict(manifest.get("settings")),
            conversation_log=ConversationLog.from_dict(conversation_data),
            tracks=[Track.from_dict(item) for item in tracks_data.get("tracks") or []],
            mix=Mix.from_dict(tracks_data.get("mix")),
            dirty=False,
        )
        project.ensure_dirs()
        return project
