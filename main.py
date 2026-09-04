import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QShowEvent, QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMenuBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app_context import APP_NAME, AppContext, Settings
from audio.io import EXPORT_FORMATS, export_file_filter, probe, save
from audio.render import (
    RenderCache,
    estimate_mix_duration_ms,
    estimate_track_duration_ms,
    render_mix,
    render_track,
)
from gui.dialogs.align_dialog import AlignDialog, AlignTrack
from gui.dialogs.channels_dialog import ChannelsDialog
from gui.dialogs.cut_dialog import CutDialog
from gui.dialogs.fade_dialog import FadeDialog
from gui.dialogs.lyrics_editor_dialog import LyricsEditorDialog
from gui.dialogs.preferences_dialog import PreferencesDialog
from gui.dialogs.shortcuts_dialog import ShortcutsDialog
from gui.dialogs.speed_dialog import SpeedDialog
from gui.dialogs.timeline_help_dialog import TimelineHelpDialog
from gui.messages import ask_save_discard_cancel, ask_yes_no, silent_message
from gui.style import (
    format_clock_ms,
    parse_clock_ms,
    size_main_window,
    wheel_time_delta_ms,
)
from gui.widgets.chat_window import ChatWindow, PromptSubmission
from gui.widgets.editing_area import EditingArea
from gui.widgets.transcript_area import TranscriptView
from gui.workers import FnWorker
from llm.base import GenerationRequest
from llm.lyria3 import Lyria3Provider
from llm.translate import translate_lrc
from workspaces.ingest import generation_history, persist_generation
from workspaces.models import Mix, MixClip, OriginalMedia, Track, \
    TrackSource
from workspaces.project import Project
from workspaces.transcript import decode_text, dump_lrc, parse_imported_lrc

NO_PROJECT_WARNING = ("No project is open. Use File → New Project or File → Open Project "
                      "before generating.")
NO_API_KEY_WARNING = ("No Gemini API key is set. Add a Google AI Studio API key in Edit "
                      "→ Settings.")
bcp_47_lang_codes = {}
bcp_47_lang_codes_inv = {}
with open("bcp_47.txt", "r", encoding="utf-8") as f:
    lines = f.readlines()
for line in lines:
    if (not line) or line.startswith("#"):
        continue
    lang_name, lang_code = line.split(",", 1)
    lang_name = lang_name.strip()
    lang_code = lang_code.strip()
    bcp_47_lang_codes[lang_code] = lang_name
    bcp_47_lang_codes_inv[lang_name] = lang_code


def _resolve_export_destination(path: str, selected_filter: str = "") -> tuple[Path, str]:
    dest = Path(path)
    fmt = dest.suffix.lstrip(".").lower()
    if fmt in EXPORT_FORMATS:
        return dest, fmt
    selected = selected_filter.lower()
    for name in EXPORT_FORMATS:
        if f"*.{name}" in selected:
            return dest.with_suffix(f".{name}"), name
    return dest.with_suffix(".wav"), "wav"


def _op_spec(name: str, values: tuple) -> dict:
    """Turn a dialog's values tuple into a stored operation spec."""
    if name == "cut":
        start, end, mode = values
        return {"op": "cut", "start_ms": start, "end_ms": end, "mode": mode}
    if name in ("fade_in", "fade_out"):
        duration, shape = values
        return {"op": name, "duration_ms": duration, "shape": shape}
    if name == "speed":
        ratio, preserve = values
        return {"op": "speed", "ratio": ratio, "preserve_pitch": preserve}
    layout_name, pan = values
    spec = {"op": "channels", "target_layout": layout_name}
    if layout_name == "stereo":
        spec["pan"] = pan
    return spec


def _uses_native_wheel(widget: QWidget) -> bool:
    node: QWidget | None = widget
    while node is not None:
        if isinstance(node, QAbstractScrollArea):
            vertical = node.verticalScrollBar()
            horizontal = node.horizontalScrollBar()
            if vertical is not None and vertical.maximum() > 0:
                return True
            if horizontal is not None and horizontal.maximum() > 0:
                return True
        node = node.parentWidget()
    return False


def _align_track(project: Project, track: Track) -> AlignTrack:
    clip = project.mix.clip_for_track(track.id)
    return AlignTrack(
        track_id=track.id,
        name=track.name,
        offset_ms=int(clip.offset_ms) if clip else 0,
        duration_ms=estimate_track_duration_ms(track),
    )


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._cache = RenderCache()
        self._pool = QThreadPool.globalInstance()
        self._workers: list[FnWorker] = []
        self._play_track_id: str | None = None
        self._autoplay_next = False
        self._chat_window: ChatWindow | None = None
        self.setWindowTitle(APP_NAME)
        size_main_window(self)

        root_layout = QVBoxLayout()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.conversation = TranscriptView()
        self.editing = EditingArea()
        self.splitter.addWidget(self.conversation)
        self.splitter.addWidget(self.editing)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        # Stretch factors only apply on later resizes; seed a 1:2 split now.
        self.splitter.setSizes([1, 2])
        self._pending_default_split = True

        self.conversation.chat_requested.connect(self._open_chat)
        self.conversation.language_changed.connect(self._on_language_changed)
        self.conversation.cue_seek_requested.connect(self._on_cue_seek)
        self.editing.edit_requested.connect(self._on_edit)
        self.editing.player.mixed_requested.connect(self._play_mixed)
        self.editing.player.position_changed.connect(self._on_playhead)
        self.editing.timeline.clip_changed.connect(self._on_offset)
        self.editing.timeline.track_moved.connect(self._on_track_moved)
        self.editing.timeline.track_selected.connect(self._on_track_selected)
        self.editing.timeline.track_activated.connect(self._set_play_target)
        self.editing.timeline.seek_requested.connect(self.editing.player.set_position)
        self.editing.timeline.delete_requested.connect(self._delete_selected_track)

        self._build_menus(root_layout)
        root_layout.addWidget(self.splitter)
        central = QWidget()
        central.setLayout(root_layout)
        self.setCentralWidget(central)
        self._sync_actions()
        self._show_session_warnings()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _reload_tracks(self) -> None:
        project = self._ctx.current_project
        if project is None:
            self.editing.timeline.reload([], Mix())
            return
        self.editing.timeline.reload(project.tracks, project.mix)
        self.editing.timeline.set_selected(self._selected_track_id())

    def _build_menus(self, layout: QVBoxLayout) -> None:
        menu = QMenuBar(self)

        new_project = QAction("&New Project", self)
        new_project.setShortcut("Ctrl+N")
        new_project.triggered.connect(self._new_project)
        open_project = QAction("&Open Project", self)
        open_project.setShortcut("Ctrl+O")
        open_project.triggered.connect(self._open_project)
        save = QAction("&Save", self)
        save.setShortcut("Ctrl+S")
        save.triggered.connect(self._save)
        save_as = QAction("Save &As...", self)
        save_as.setShortcut("Ctrl+Shift+S")
        save_as.triggered.connect(self._save_as)
        self._recent_menu = QMenu("Recent Projects", self)
        import_audio = QAction("&Import Audio...", self)
        import_audio.triggered.connect(self._import_audio)
        export_mix = QAction("&Export Mix...", self)
        export_mix.triggered.connect(self._export_mix)
        close_project = QAction("&Close Project", self)
        close_project.triggered.connect(self._close_project)
        exit_ = QAction("E&xit", self)
        exit_.setShortcut("Ctrl+Q")
        exit_.triggered.connect(self.close)
        settings = QAction("&Settings...", self)
        settings.triggered.connect(self._settings)
        project_menu = QMenu("&Project", self)
        project_menu.addActions([new_project, open_project, save, save_as])
        project_menu.addMenu(self._recent_menu)
        project_menu.addAction(close_project)
        project_menu.addSeparator()
        project_menu.addActions([settings, exit_])
        menu.addMenu(project_menu)

        undo = QAction("&Undo", self)
        undo.setShortcut("Ctrl+Z")
        undo.triggered.connect(self._undo)
        redo = QAction("&Redo", self)
        redo.setShortcut("Ctrl+Y")
        redo.triggered.connect(self._redo)

        open_chat = QAction("&Chat with Lyria", self)
        open_chat.setShortcut("Ctrl+L")
        open_chat.triggered.connect(self._open_chat)
        compose_menu = QMenu("&Compose", self)
        compose_menu.addActions([undo, redo])
        compose_menu.addSeparator()
        compose_menu.addActions([import_audio, open_chat])
        compose_menu.addSeparator()
        compose_menu.addActions([export_mix])
        menu.addMenu(compose_menu)

        self._import_lyrics_action = QAction("&Import...", self)
        self._import_lyrics_action.setToolTip("Import LRC lyrics for the selected track")
        self._import_lyrics_action.triggered.connect(self._import_transcript)
        self._export_lyrics_action = QAction("&Export...", self)
        self._export_lyrics_action.setToolTip("Export the current lyrics as LRC")
        self._export_lyrics_action.triggered.connect(self._export_transcript)
        self._translate_lyrics_action = QAction("&Translate", self)
        self._translate_lyrics_action.setToolTip("Translate lyrics with the model set in Settings")
        self._translate_lyrics_action.triggered.connect(self._translate_transcript)
        self._edit_lyrics_action = QAction("&Synchronize...", self)
        self._edit_lyrics_action.setToolTip(
            "Stamp lyric timestamps while the track plays"
        )
        self._edit_lyrics_action.triggered.connect(self._edit_lyrics)
        lyric_menu = QMenu("&Lyrics", self)
        lyric_menu.addActions([
            self._import_lyrics_action,
            self._export_lyrics_action,
            self._translate_lyrics_action,
            self._edit_lyrics_action,
        ])
        menu.addMenu(lyric_menu)

        toggle_transcript = QAction("Toggle &Transcript panel", self)
        toggle_transcript.setShortcut("Ctrl+T")
        toggle_transcript.setCheckable(True)
        toggle_transcript.setChecked(True)
        toggle_transcript.toggled.connect(lambda on: self.conversation.setVisible(on))
        reset_layout = QAction("&Reset Layout", self)
        reset_layout.triggered.connect(self._reset_layout)
        view_menu = QMenu("&View", self)
        view_menu.addActions([reset_layout, toggle_transcript])
        menu.addMenu(view_menu)

        shortcuts = QAction("&Shortcuts", self)
        shortcuts.setShortcut("F1")
        shortcuts.triggered.connect(lambda: ShortcutsDialog(self).exec())
        timeline_help = QAction("&Time line", self)
        timeline_help.triggered.connect(lambda: TimelineHelpDialog(self).exec())
        help_menu = QMenu("&Help", self)
        help_menu.addActions([shortcuts, timeline_help])
        menu.addMenu(help_menu)

        layout.setMenuBar(menu)

        # Everything that needs an open project to do anything useful.
        self._project_actions = [
            save,
            save_as,
            close_project,
            undo,
            redo,
            import_audio,
            export_mix,
        ]
        self._undo_action = undo
        self._redo_action = redo
        self._rebuild_recent()

    def _rebuild_recent(self) -> None:
        self._recent_menu.clear()
        paths = self._ctx.settings.recent_projects
        if not paths:
            empty = QAction("(none)", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)
            return
        for path in paths:
            action = QAction(path, self)
            action.triggered.connect(lambda _checked=False, p=path: self._open_path(Path(p)))
            self._recent_menu.addAction(action)

    def _sync_actions(self) -> None:
        """Refresh action enablement, the window title, and the timeline."""
        project = self._ctx.current_project
        has_project = project is not None
        for action in self._project_actions:
            action.setEnabled(has_project)
        self.editing.player.set_mixed_enabled(has_project)
        if project is None:
            self.setWindowTitle(APP_NAME)
        else:
            self._undo_action.setEnabled(project.undo_stack.can_undo())
            self._redo_action.setEnabled(project.undo_stack.can_redo())
            self.setWindowTitle(
                f"{project.name}{'*' if project.dirty else ''} — {APP_NAME}"
            )
        self._reload_tracks()

    def _set_transcript_actions_enabled(self, *, can_import: bool, can_export: bool, can_translate: bool) -> None:
        self._can_import = can_import
        self._can_export = can_export
        self._can_translate = can_translate
        self._apply_transcript_actions_enabled()

    def _apply_transcript_actions_enabled(self) -> None:
        busy = getattr(self, "_transcript_busy", False)
        idle = not busy
        self._import_lyrics_action.setEnabled(idle and getattr(self, "_can_import", False))
        self._export_lyrics_action.setEnabled(idle and getattr(self, "_can_export", False))
        self._translate_lyrics_action.setEnabled(idle and getattr(self, "_can_translate", False))
        self._edit_lyrics_action.setEnabled(idle and getattr(self, "_can_export", False))
        self.conversation.set_busy(busy)
        self._translate_lyrics_action.setText("Translating..." if busy else "&Translate")

    def _set_transcript_busy(self, busy: bool) -> None:
        self._transcript_busy = busy
        self._apply_transcript_actions_enabled()

    def _set_project(self, project: Project | None) -> None:
        self.editing.player.stop()
        self._cache.invalidate()
        self._play_track_id = None
        self.conversation.clear_warnings()
        if self._chat_window is not None:
            self._chat_window.flush_model()
            self._chat_window.clear_composer()
            self._chat_window.clear_warnings()
        self._ctx.current_project = project
        if project is not None:
            self._ctx.settings.remember_project(project.root)
            self._ctx.settings.save()
            self._rebuild_recent()
        self._sync_actions()
        self.editing.player.set_clip(None)
        self.conversation.set_transcript(None, [])
        self._set_transcript_actions_enabled(
            can_import=False, can_export=False, can_translate=False
        )
        if project is not None:
            self._queue_render()
        self._show_session_warnings()
        if self._chat_window is not None:
            self._chat_window.reload()

    def _show_session_warnings(self) -> None:
        if self._ctx.current_project is None:
            self.conversation.add_warning(NO_PROJECT_WARNING)
            if self._chat_window is not None:
                self._chat_window.add_warning(NO_PROJECT_WARNING)
        if not self._ctx.settings.gemini_api_key:
            self.conversation.add_warning(NO_API_KEY_WARNING)
            if self._chat_window is not None:
                self._chat_window.add_warning(NO_API_KEY_WARNING)

    def _confirm_discard(self) -> bool:
        project = self._ctx.current_project
        if project is None or not project.dirty:
            return True
        choice = ask_save_discard_cancel(
            self,
            "Unsaved changes",
            "Save the current project before continuing?",
        )
        if choice == "save":
            return self._save()
        return choice == "discard"

    def _new_project(self) -> None:
        if not self._confirm_discard():
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose an empty folder for the project")
        if not folder:
            return
        root = Path(folder)
        occupied = [p for p in root.iterdir() if p.name not in {".DS_Store", "Thumbs.db"}]
        if occupied and not (root / "project.json").is_file():
            if not ask_yes_no(
                self,
                "Folder not empty",
                "This folder is not empty. Create a project here anyway?",
            ):
                return
        settings = self._ctx.settings
        project = Project.create(
            root,
            model=settings.composition_model,
            export_format=settings.export_format,
        )
        self._set_project(project)

    def _open_project(self) -> None:
        if not self._confirm_discard():
            return
        folder = QFileDialog.getExistingDirectory(self, "Open Project")
        if folder:
            self._open_path(Path(folder))

    def _open_path(self, root: Path) -> None:
        try:
            project = Project.load(root)
        except (OSError, ValueError) as exc:
            silent_message(self, "warn", "Project", str(exc))
            return
        self._set_project(project)

    def _save(self) -> bool:
        project = self._ctx.current_project
        if project is None:
            return False
        try:
            project.save()
        except OSError as exc:
            silent_message(self, "critical", "Save", str(exc))
            return False
        self._sync_actions()
        return True

    def _save_as(self) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Save project as (choose empty folder)")
        if not folder:
            return
        dest = Path(folder)
        if dest.resolve() == project.root.resolve():
            self._save()
            return
        if any(dest.iterdir()) and not ask_yes_no(self, "Save As", "Destination is not empty. Copy into it?"):
            return
        try:
            shutil.copytree(project.root, dest, dirs_exist_ok=True)
            copied = Project.load(dest)
        except (OSError, ValueError) as exc:
            silent_message(self, "critical", "Save As", str(exc))
            return
        self._set_project(copied)

    def _close_project(self) -> None:
        if not self._confirm_discard():
            return
        self._set_project(None)

    def _import_audio(self) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        path, _ok = QFileDialog.getOpenFileName(
            self,
            "Import Audio",
            filter="Audio (*.wav *.mp3 *.flac *.ogg *.oga)",
        )
        if not path:
            return
        source = Path(path)
        media_id = f"aud-{uuid.uuid4().hex}"
        dest = project.copy_media(source, media_id, "audio")
        try:
            sample_rate, channels, duration_ms = probe(dest)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            silent_message(self, "warn", "Import", str(exc))
            dest.unlink(missing_ok=True)
            return
        project.snapshot_edits()
        track = Track(
            id=f"trk-{uuid.uuid4().hex}",
            name=source.stem,
            source=TrackSource(type="imported"),
            media_id=media_id,
            original=OriginalMedia(
                path=project.rel(dest),
                sample_rate=sample_rate,
                channels=channels,
                duration_ms=duration_ms,
            ),
        )
        project.add_track(track)
        project.save()
        self._cache.invalidate()
        self._sync_actions()
        self._queue_render()

    def _export_mix(self) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export Mix",
            f"{project.name}.wav",
            export_file_filter(),
        )
        if not path:
            return
        dest, fmt = _resolve_export_destination(path, selected)

        def work() -> Path:
            save(
                self._render_mix_clip(project),
                dest,
                fmt=fmt,
                mp3_quality=project.export_mp3_quality,
            )
            return dest

        self._run(work, lambda saved: silent_message(self, "info", "Export", f"Wrote {saved}"))

    def _run(self, fn, on_ok, on_err=None) -> None:
        worker = FnWorker(fn)
        self._workers.append(worker)

        def ok(result) -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            on_ok(result)

        def err(message: str) -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            if on_err:
                on_err(message)
            else:
                silent_message(self, "warn", APP_NAME, message)

        # run() emits from a pool thread; queue back so GUI objects stay on this thread.
        worker.signals.finished.connect(ok, Qt.ConnectionType.QueuedConnection)
        worker.signals.error.connect(err, Qt.ConnectionType.QueuedConnection)
        self._pool.start(worker)

    def _open_chat(self) -> None:
        if self._chat_window is None:
            window = ChatWindow(self._ctx, self)
            window.generate_requested.connect(self._on_generate)
            window.play_track_requested.connect(self._on_play_track_from_chat)
            window.save_track_requested.connect(self._save_track_as)
            window.conversations_changed.connect(self._sync_actions)
            window.destroyed.connect(self._on_chat_destroyed)
            self._chat_window = window
        self._chat_window.clear_warnings()
        self._show_session_warnings()
        self._chat_window.reload()
        self._chat_window.show()
        self._chat_window.raise_()
        self._chat_window.activateWindow()
        self._chat_window.composer.prompt.setFocus()

    def _on_chat_destroyed(self, _obj=None) -> None:
        self._chat_window = None

    def _on_play_track_from_chat(self, track_id: str) -> None:
        if not track_id:
            return
        self._select_track(track_id)
        self._set_play_target(track_id)

    def _on_generate(self, submission: PromptSubmission) -> None:
        project = self._ctx.current_project
        chat = self._chat_window
        if project is None:
            self.conversation.add_warning(NO_PROJECT_WARNING)
            if chat is not None:
                chat.add_warning(NO_PROJECT_WARNING)
            return
        if not self._ctx.settings.gemini_api_key:
            self.conversation.add_warning(NO_API_KEY_WARNING)
            if chat is not None:
                chat.add_warning(NO_API_KEY_WARNING)
            return
        model_id = submission.model.strip() or self._ctx.settings.composition_model
        if not model_id:
            silent_message(self, "warn", "Generation", "No composition model is set.")
            return
        provider = Lyria3Provider(
            self._ctx.settings,
            model_id=model_id,
        )
        history = []
        conversation = project.conversation_by_id(submission.conversation_id)
        if submission.regenerate and conversation is not None:
            history = generation_history(project, conversation)
        request = GenerationRequest(
            prompt=submission.prompt,
            images=[] if history else [path.read_bytes() for path in submission.images],
            image_mimes=[] if history else list(submission.image_mimes),
            negative_prompt=submission.negative_prompt,
            history=history,
        )
        if chat is not None:
            chat.set_busy(True)

        def work():
            return provider.generate(request)

        def ok_result(result) -> None:
            if self._chat_window is not None:
                self._chat_window.set_busy(False)
            try:
                track_ids = persist_generation(
                    project,
                    provider,
                    request,
                    result,
                    image_paths=submission.images,
                    conversation_id=submission.conversation_id or None,
                    include_user_turn=not submission.regenerate,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                silent_message(self, "warn", "Generation", str(exc))
                return
            if self._chat_window is not None:
                if not submission.regenerate:
                    self._chat_window.clear_composer()
                self._chat_window.reload()
            self._cache.invalidate()
            self._sync_actions()
            if track_ids:
                self._select_track(track_ids[-1])
                self._set_play_target(track_ids[-1])
            else:
                self._queue_render()

        def err(message: str) -> None:
            if self._chat_window is not None:
                self._chat_window.set_busy(False)
            silent_message(self, "warn", "Generation", message)

        self._run(work, ok_result, err)

    def _selected_track(self) -> Track | None:
        """The single edit target, or None when nothing usable is selected."""
        project = self._ctx.current_project
        track_id = self._selected_track_id()
        if project is None or not track_id:
            return None
        return project.track_by_id(track_id)

    def _require_track(self) -> Track | None:
        track = self._selected_track()
        if track is None:
            silent_message(self, "info", "Selector", "Select a track first.")
        return track

    def _on_edit(self, name: str) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        simple = {
            "align": self._align_selected,
            "export_audio": self._export_selected_track,
            "delete": self._delete_selected_track,
            "rename": self._rename_selected,
            "start": self._edit_start,
            "gain": self._edit_gain,
            "mute": self._toggle_mute_selected,
            "duplicate": self._duplicate_selected,
        }
        handler = simple.get(name)
        if handler is not None:
            handler()
            return
        track = self._require_track()
        if track is None:
            return
        if name == "reverse":
            self._append_op(track, {"op": "reverse"})
            return
        if name == "clear_edits":
            project = self._ctx.current_project
            if project is None or not track.operations:
                return
            if not ask_yes_no(
                    self,
                    "Clear edits",
                    "Discard the operation chain on this track? The original audio is untouched.",
            ):
                return
            project.snapshot_edits()
            track.operations.clear()
            self._cache.invalidate(track.id)
            project.mark_dirty()
            self._sync_actions()
            self._queue_render()
            return
        dialog = None
        match name:
            case "cut":
                dialog = CutDialog(self, track.original.duration_ms)
            case "fade_in":
                dialog = FadeDialog("Fade In", self)
            case "fade_out":
                dialog = FadeDialog("Fade Out", self)
            case "speed":
                dialog = SpeedDialog(self)
            case "channels":
                dialog = ChannelsDialog(project.default_channel_layout, self)
        if dialog is None or dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._append_op(track, _op_spec(name, dialog.values()))

    def _append_op(self, track: Track, spec: dict) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        project.snapshot_edits()
        track.operations.append(spec)
        self._cache.invalidate(track.id)
        project.mark_dirty()
        self._sync_actions()
        self._queue_render()

    def _align_selected(self) -> None:
        project = self._ctx.current_project
        track = self._require_track()
        if project is None or track is None:
            return
        references = [
            _align_track(project, item)
            for item in project.tracks
            if item.id != track.id
        ]
        if not references:
            silent_message(
                self,
                "info",
                "Align",
                "There is no other track to align against. Leave at least one "
                "track unselected as the reference.",
            )
            return
        dialog = AlignDialog([_align_track(project, track)], references, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        offsets = dialog.offsets()
        if not offsets:
            return
        project.snapshot_edits()
        for track_id, offset_ms in offsets.items():
            clip = project.mix.clip_for_track(track_id)
            if clip is None:
                clip = MixClip(track_id=track_id)
                project.mix.clips.append(clip)
            clip.offset_ms = offset_ms
        project.mark_dirty()
        self._sync_actions()
        self._queue_render()

    def _export_selected_track(self) -> None:
        track = self._require_track()
        if track is not None:
            self._save_track_as(track.id)

    def _delete_selected_track(self) -> None:
        project = self._ctx.current_project
        track = self._selected_track()
        if project is None or track is None:
            return
        ids = [item.id for item in project.tracks]
        index = ids.index(track.id)
        neighbours = ids[index + 1 :] + ids[:index][::-1]
        project.snapshot_edits()
        project.remove_track(track.id)
        self._cache.invalidate(track.id)
        if self._play_track_id == track.id:
            self._play_track_id = None
        self._sync_actions()
        self._select_track(neighbours[0] if neighbours else "")
        self._queue_render()

    def _import_transcript(self) -> None:
        project = self._ctx.current_project
        track = self._selected_track()
        if project is None or track is None:
            silent_message(self, "info", "Transcript", "Select a track first.")
            return
        path, _ok = QFileDialog.getOpenFileName(
            self,
            "Import LRC",
            filter="Lyrics (*.lrc *.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            silent_message(self, "warn", "Import", str(exc))
            return
        cues = parse_imported_lrc(decode_text(data), track.original.duration_ms)
        if not cues:
            silent_message(
                self,
                "warn",
                "Import",
                "This file does not contain LRC timestamps such as [00:16.00].",
            )
            return
        lang_code_list = list(bcp_47_lang_codes.keys())
        current_idx = lang_code_list.index(self.conversation.language.currentText())
        language, ok = QInputDialog.getItem(
            self,
            "Import LRC",
            "Language code:",
            list(bcp_47_lang_codes.values()),
            current_idx,
            False,
        )
        if not ok or not language.strip():
            return
        language = language.strip()
        project.snapshot_edits()
        project.save_transcript(track, language, "imported", cues)
        project.save()
        self._sync_actions()
        self._refresh_transcript(track, preferred=language)

    def _export_transcript(self) -> None:
        project = self._ctx.current_project
        track = self._selected_track()
        if project is None or track is None:
            silent_message(self, "info", "Transcript", "Select a track first.")
            return
        language = self.conversation.language.currentText().strip()
        transcript = project.load_transcript(track, language) if language else None
        if transcript is None or not transcript.cues:
            silent_message(self, "info", "Export", "This track has no lyrics to export.")
            return
        path, _ok = QFileDialog.getSaveFileName(
            self,
            "Export LRC",
            f"{track.name}.lrc",
            filter="LRC (*.lrc);;Text (*.txt)",
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix.lower() not in {".lrc", ".txt"}:
            dest = dest.with_suffix(".lrc")
        try:
            dest.write_text(
                dump_lrc(transcript.cues, title=track.name, language=language),
                encoding="utf-8",
            )
        except OSError as exc:
            silent_message(self, "warn", "Export", str(exc))
            return
        silent_message(self, "info", "Export", f"Wrote {dest}")

    def _edit_lyrics(self) -> None:
        project = self._ctx.current_project
        track = self._selected_track()
        if project is None or track is None:
            silent_message(self, "info", "Transcript", "Select a track first.")
            return
        language = self.conversation.language.currentText().strip()
        transcript = project.load_transcript(track, language) if language else None
        if transcript is None or not transcript.cues:
            silent_message(self, "info", "Edit", "This track has no lyrics to edit.")
            return
        self.editing.player.pause()
        self._set_transcript_busy(True)
        cues = list(transcript.cues)
        title = track.name

        def work():
            return render_track(track, project.root, cache=self._cache)

        def ok_result(clip) -> None:
            self._set_transcript_busy(False)
            dialog = LyricsEditorDialog(cues, clip, title=title, parent=self)
            if dialog.exec() != dialog.DialogCode.Accepted:
                return
            edited = dialog.cues()
            if not edited:
                return
            project.snapshot_edits()
            project.save_transcript(track, language, "imported", edited)
            project.save()
            self._sync_actions()
            self._refresh_transcript(track, preferred=language)

        def err(message: str) -> None:
            self._set_transcript_busy(False)
            silent_message(self, "warn", "Edit", message)

        self._run(work, ok_result, err)

    def _translate_transcript(self) -> None:
        project = self._ctx.current_project
        track = self._selected_track()
        if project is None or track is None:
            silent_message(self, "info", "Transcript", "Select a track first.")
            return
        api_key = self._ctx.settings.gemini_api_key
        if not api_key:
            self.conversation.add_warning(NO_API_KEY_WARNING)
            silent_message(self, "warn", "Translate", NO_API_KEY_WARNING)
            return
        source_language = self.conversation.language.currentText().strip()
        transcript = (
            project.load_transcript(track, source_language) if source_language else None
        )
        if transcript is None or not transcript.cues:
            silent_message(self, "info", "Translate", "This track has no lyrics to translate.")
            return
        target_name, ok = QInputDialog.getItem(
            self,
            "Translate lyrics",
            "Translate into:",
            list(bcp_47_lang_codes.values()),
            0,
            False,
        )
        if not ok or not target_name:
            return
        target = bcp_47_lang_codes_inv[target_name]
        existing = next((item for item in track.transcripts if item.language == target), None)
        if existing is not None and not ask_yes_no(
            self,
            "Translate",
            f"Replace the existing “{target}” lyrics for this track?",
        ):
            return
        model_id = self._ctx.settings.translation_model
        if not model_id:
            silent_message(self, "warn", "Translate", "No translation model is set.")
            return
        cues = list(transcript.cues)
        title = track.name
        self._set_transcript_busy(True)

        def work():
            return translate_lrc(
                cues,
                target,
                api_key=api_key,
                model_id=model_id,
                title=title,
                source_language=source_language,
            )

        def ok_result(translated) -> None:
            self._set_transcript_busy(False)
            project.snapshot_edits()
            project.save_transcript(track, target, "translated", translated)
            project.save()
            self._sync_actions()
            self._refresh_transcript(track, preferred=target)

        def err(message: str) -> None:
            self._set_transcript_busy(False)
            silent_message(self, "warn", "Translate", message)

        self._run(work, ok_result, err)

    def _on_offset(self, track_id: str, offset_ms: int) -> None:
        self._update_clip(track_id, offset_ms=offset_ms)

    def _update_clip(
        self,
        track_id: str,
        *,
        offset_ms: int | None = None,
        gain_db: float | None = None,
        mute: bool | None = None,
    ) -> None:
        """Single write path for the per-clip mix state."""
        project = self._ctx.current_project
        if project is None or not track_id:
            return
        clip = project.mix.clip_for_track(track_id)
        current = (clip.offset_ms, clip.gain_db, clip.mute) if clip else (0, 0.0, False)
        wanted = (
            current[0] if offset_ms is None else int(offset_ms),
            current[1] if gain_db is None else round(gain_db, 1),
            current[2] if mute is None else bool(mute),
        )
        if clip is not None and wanted == current:
            return
        project.snapshot_edits()
        if clip is None:
            clip = MixClip(track_id=track_id)
            project.mix.clips.append(clip)
        clip.offset_ms, clip.gain_db, clip.mute = wanted
        project.mark_dirty()
        self._sync_actions()
        # Mute and gain only shape the mix. Offset also places a single-track
        # preview on the project timeline, so reload that playback too.
        if self._play_track_id is None or offset_ms is not None:
            self._queue_render()

    def _on_track_selected(self, track_id: str) -> None:
        """Selection picks the edit target only.

        It must not touch the transport: swapping the player source here would
        cut off whatever is currently playing.
        """
        project = self._ctx.current_project
        if project is None:
            return
        track = project.track_by_id(track_id) if track_id else None
        if track is None:
            self.conversation.set_transcript(None, [])
            self._set_transcript_actions_enabled(
                can_import=False, can_export=False, can_translate=False
            )
            return
        self._refresh_transcript(track)

    def _set_play_target(self, track_id: str, *, autoplay: bool = False) -> None:
        """Choose what the player loads: one track, or the mix when empty."""
        self._play_track_id = track_id or None
        if autoplay:
            self._autoplay_next = True
        self._queue_render()

    def _selected_track_id(self) -> str:
        return self.editing.timeline.selected_track_id()

    def _select_track(self, track_id: str) -> None:
        self.editing.timeline.set_selected(track_id)
        self._on_track_selected(track_id)

    def _rename_selected(self) -> None:
        project = self._ctx.current_project
        track = self._require_track()
        if project is None or track is None:
            return
        name, ok = QInputDialog.getText(self, "Rename track", "Name:", text=track.name)
        name = name.strip()
        if not ok or not name or name == track.name:
            return
        project.snapshot_edits()
        track.name = name
        project.mark_dirty()
        self._sync_actions()

    def _edit_start(self) -> None:
        project = self._ctx.current_project
        track = self._require_track()
        if project is None or track is None:
            return
        clip = project.mix.clip_for_track(track.id)
        text, ok = QInputDialog.getText(
            self,
            "Start",
            "Start as MM:SS.mmm, or a plain millisecond count:",
            text=format_clock_ms(clip.offset_ms if clip else 0),
        )
        if not ok:
            return
        offset = parse_clock_ms(text)
        if offset is None:
            silent_message(self, "warn", "Start", f"“{text.strip()}” is not a valid time.")
            return
        self._update_clip(track.id, offset_ms=offset)

    def _edit_gain(self) -> None:
        project = self._ctx.current_project
        track = self._require_track()
        if project is None or track is None:
            return
        clip = project.mix.clip_for_track(track.id)
        gain, ok = QInputDialog.getDouble(
            self,
            "Gain",
            "Gain (dB, in [-60, 24]):",
            float(clip.gain_db) if clip else 0.0,
            -60,
            24,
            1,
        )
        if ok:
            self._update_clip(track.id, gain_db=gain)

    def _toggle_mute_selected(self) -> None:
        project = self._ctx.current_project
        track = self._require_track()
        if project is None or track is None:
            return
        clip = project.mix.clip_for_track(track.id)
        self._update_clip(track.id, mute=not (clip is not None and clip.mute))

    def _duplicate_selected(self) -> None:
        project = self._ctx.current_project
        source = self._require_track()
        if project is None or source is None:
            return
        index = project.tracks.index(source)
        clip = project.mix.clip_for_track(source.id)
        project.snapshot_edits()
        # The copy reuses the same media file; only the edit chain is cloned.
        copy = Track(
            id=f"trk-{uuid.uuid4().hex}",
            name=f"{source.name} copy",
            source=replace(source.source),
            media_id=source.media_id,
            original=replace(source.original),
            operations=[dict(spec) for spec in source.operations],
        )
        project.add_track(copy, offset_ms=int(clip.offset_ms) if clip else 0)
        new_clip = project.mix.clip_for_track(copy.id)
        if new_clip is not None and clip is not None:
            new_clip.gain_db = clip.gain_db
            new_clip.mute = clip.mute
        project.move_track(copy.id, index + 1)
        self._sync_actions()
        self._select_track(copy.id)
        self._queue_render()

    def _refresh_transcript(self, track: Track, preferred: str | None = None) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        languages = [item.language for item in track.transcripts]
        current = preferred or self.conversation.language.currentText()
        if current not in languages:
            current = languages[0] if languages else ""
        transcript = project.load_transcript(track, current) if current else None
        self.conversation.set_transcript(transcript, languages, preferred=current or None)
        has_cues = transcript is not None and bool(transcript.cues)
        self._set_transcript_actions_enabled(
            can_import=True,
            can_export=has_cues,
            can_translate=has_cues,
        )

    def _on_language_changed(self, language: str) -> None:
        project = self._ctx.current_project
        track_id = self._selected_track_id()
        if project is None or not track_id or not language:
            return
        track = project.track_by_id(track_id)
        if track is None:
            return
        loaded = project.load_transcript(track, language)
        self.conversation.set_transcript(
            loaded,
            [item.language for item in track.transcripts],
            preferred=language,
        )
        has_cues = loaded is not None and bool(loaded.cues)
        self._set_transcript_actions_enabled(
            can_import=True,
            can_export=has_cues,
            can_translate=has_cues,
        )

    def _on_cue_seek(self, start_ms: int) -> None:
        self.editing.player.set_position(start_ms + self._selected_track_offset_ms())

    def _selected_track_offset_ms(self) -> int:
        project = self._ctx.current_project
        track_id = self._selected_track_id()
        if project is None or not track_id:
            return 0
        clip = project.mix.clip_for_track(track_id)
        return int(clip.offset_ms) if clip else 0

    def _on_playhead(self, position_ms: int) -> None:
        self.conversation.highlight_at(position_ms - self._selected_track_offset_ms())
        self.editing.timeline.set_position(position_ms)

    def _on_track_moved(self, track_id: str, index: int) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        current = next((i for i, item in enumerate(project.tracks) if item.id == track_id), None)
        if current is None or current == index:
            # The timeline previewed the drop; put its list back the way it was.
            self._reload_tracks()
            return
        project.snapshot_edits()
        project.move_track(track_id, index)
        self._sync_actions()

    def _play_mixed(self) -> None:
        self._select_track("")
        self._set_play_target("", autoplay=True)

    def _save_track_as(self, track_id: str) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        track = project.track_by_id(track_id)
        if track is None:
            return
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export track audio",
            f"{track.name}.wav",
            export_file_filter(),
        )
        if not path:
            return
        dest, fmt = _resolve_export_destination(path, selected)

        def work() -> Path:
            clip = render_track(track, project.root, cache=self._cache)
            save(
                clip,
                dest,
                fmt=fmt,
                mp3_quality=project.export_mp3_quality,
            )
            return dest

        self._run(work, lambda saved: silent_message(self, "info", "Save", f"Wrote {saved}"))

    def _render_mix_clip(self, project: Project):
        return render_mix(
            project.tracks,
            project.mix,
            project.root,
            sample_rate=project.sample_rate,
            clip_protection=project.clip_protection,
            cache=self._cache,
        )

    def _queue_render(self) -> None:
        project = self._ctx.current_project
        if project is None:
            self.editing.player.set_clip(None)
            return
        track_id = self._play_track_id
        autoplay = self._autoplay_next
        self._autoplay_next = False

        def work():
            timeline_ms = estimate_mix_duration_ms(project.tracks, project.mix)
            if not track_id:
                return self._render_mix_clip(project), project.mix.name, 0, timeline_ms
            track = project.track_by_id(track_id)
            if track is None:
                raise RuntimeError("Track no longer exists.")
            clip = render_track(track, project.root, cache=self._cache)
            mix_clip = project.mix.clip_for_track(track.id)
            return clip, track.name, mix_clip.offset_ms if mix_clip else 0, timeline_ms

        def ok(result) -> None:
            clip, label, offset_ms, timeline_ms = result
            self.editing.player.set_clip(
                clip, label, offset_ms=offset_ms, timeline_ms=timeline_ms
            )
            if autoplay:
                self.editing.player.play()

        self._run(work, ok)

    def _undo(self) -> None:
        project = self._ctx.current_project
        if project is None or not project.undo():
            return
        self._cache.invalidate()
        self._sync_actions()
        self._queue_render()

    def _redo(self) -> None:
        project = self._ctx.current_project
        if project is None or not project.redo():
            return
        self._cache.invalidate()
        self._sync_actions()
        self._queue_render()

    def _settings(self) -> None:
        dialog = PreferencesDialog(self._ctx.settings, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        dialog.apply_to(self._ctx.settings)
        self._ctx.settings.save()
        self.conversation.clear_warnings()
        if self._chat_window is not None:
            self._chat_window.clear_warnings()
        self._show_session_warnings()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._pending_default_split:
            QTimer.singleShot(0, self._finish_default_split)

    def _finish_default_split(self) -> None:
        if not self._pending_default_split:
            return
        self._pending_default_split = False
        self._apply_split_ratio()

    def _apply_split_ratio(self) -> None:
        width = max(1, self.splitter.size().width())
        left = max(1, width // 3)
        self.splitter.setSizes([left, max(1, width - left)])

    def _reset_layout(self) -> None:
        self._apply_split_ratio()
        self.conversation.setVisible(True)
        self.conversation.cues.setVisible(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and self._handle_playhead_wheel(watched, event):
            return True
        return super().eventFilter(watched, event)

    def _handle_playhead_wheel(self, watched: QObject, event: QEvent) -> bool:
        if self._ctx.current_project is None or not self.isActiveWindow():
            return False
        if not isinstance(event, QWheelEvent) or not isinstance(watched, QWidget):
            return False
        if watched.window() is not self:
            return False
        target = self._wheel_target(watched, event)
        if _uses_native_wheel(target) or self._is_timeline_wheel(target):
            return False
        delta_ms = wheel_time_delta_ms(event)
        if delta_ms == 0:
            return False
        self._nudge_playhead(delta_ms)
        return True

    def _wheel_target(self, watched: QWidget, event: QWheelEvent) -> QWidget:
        """Resolve the widget under the pointer rather than trusting the receiver.

        A scroll area that is already at its top or bottom ignores the wheel
        event, and Qt then propagates it to the parent, which usually has no
        scrollbars of its own.
        """
        child = self.childAt(self.mapFromGlobal(event.globalPosition().toPoint()))
        return child if child is not None else watched

    def _is_timeline_wheel(self, widget: QWidget) -> bool:
        node: QWidget | None = widget
        while node is not None:
            if node is self.editing.timeline:
                return True
            node = node.parentWidget()
        return False

    def _nudge_playhead(self, delta_ms: int) -> None:
        player = self.editing.player
        timeline = self.editing.timeline
        duration = max(player.duration_ms(), timeline.duration_ms())
        current = (
            player.position_ms() if player.duration_ms() > 0 else timeline.position_ms()
        )
        new_pos = min(max(current + delta_ms, 0), duration)
        timeline.set_position(new_pos)
        player.set_position(new_pos)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self.editing.player.stop()
        if self._chat_window is not None:
            self._chat_window.close()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    with open("gui/style.css") as f:
        style = f.read()
    app.setStyleSheet(style)
    ctx = AppContext(settings=Settings.load())
    window = MainWindow(ctx)
    window.show()
    sys.exit(app.exec())
