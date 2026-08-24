"""Separate Gemini-style window: multiple Lyria conversations, history, and prompt."""



from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeyEvent, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui.messages import ask_yes_no
from gui.style import format_clock, height_for_lines, size_chat_window
from gui.widgets.conversation_view import WarningBubble
from app_context import APP_NAME, AppContext, DEFAULT_COMPOSITION_MODEL, resolve_composition_model
from workspaces.models import (
    DEFAULT_CONVERSATION_TITLE,
    Conversation,
    GenerationParams,
    Message,
    utc_now,
)
from workspaces.project import Project


@dataclass
class PromptSubmission:
    prompt: str
    images: list[Path] = field(default_factory=list)
    image_mimes: list[str] = field(default_factory=list)
    model: str = ""
    negative_prompt: str | None = None
    seed: int | None = None
    sample_count: int = 1
    conversation_id: str = ""
    regenerate: bool = False


@dataclass
class _UserDraft:
    prompt: str
    images: list[Path]
    model: str
    negative_prompt: str | None


_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class AttachmentChip(QFrame):
    removed = pyqtSignal(object)

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 2, 2)
        name = QLabel(path.name)
        name.setToolTip(str(path))
        name.setWordWrap(True)
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        remove = QToolButton()
        remove.setText("×")
        remove.setAutoRaise(True)
        remove.setToolTip("Remove this image")
        remove.clicked.connect(lambda: self.removed.emit(self.path))
        layout.addWidget(name, 1)
        layout.addWidget(remove)


class AudioChip(QFrame):
    play_requested = pyqtSignal(str)
    save_requested = pyqtSignal(str)

    def __init__(
        self,
        track_id: str,
        name: str,
        duration_ms: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.track_id = track_id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        header.addWidget(title, 1)
        if duration_ms is not None:
            header.addWidget(QLabel(format_clock(duration_ms)))
        layout.addLayout(header)
        actions = QHBoxLayout()
        load = QPushButton("Load into player")
        load.setEnabled(bool(track_id))
        load.clicked.connect(lambda: self.play_requested.emit(self.track_id))
        save = QPushButton("Save as…")
        save.setEnabled(bool(track_id))
        save.clicked.connect(lambda: self.save_requested.emit(self.track_id))
        actions.addWidget(load)
        actions.addWidget(save)
        actions.addStretch()
        layout.addLayout(actions)


class UserBubble(QFrame):
    edit_requested = pyqtSignal(str)

    def __init__(
        self,
        message: Message,
        image_paths: list[Path],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.message_id = message.id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>You</b>"))
        text = message.text().strip()
        if text:
            body = QLabel(text)
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setWordWrap(True)
            body.setMinimumWidth(0)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(body)
        if image_paths:
            thumbs = QHBoxLayout()
            for path in image_paths:
                thumbs.addWidget(_image_thumb(path))
            thumbs.addStretch()
            layout.addLayout(thumbs)
        footer = QHBoxLayout()
        if message.generation is not None:
            footer.addWidget(_model_id_label(message.generation))
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.message_id))
        footer.addWidget(self.edit_btn)
        footer.addStretch()
        layout.addLayout(footer)

    def set_actions_enabled(self, enabled: bool) -> None:
        self.edit_btn.setEnabled(enabled)


class AssistantBubble(QFrame):
    play_requested = pyqtSignal(str)
    save_requested = pyqtSignal(str)
    regenerate_requested = pyqtSignal(str)

    def __init__(
        self,
        message: Message,
        project: Project | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.message_id = message.id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Lyria</b>"))
        text = message.text().strip()
        if text:
            body = QLabel(text)
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setWordWrap(True)
            body.setMinimumWidth(0)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(body)
        for part in message.parts:
            if part.type != "audio":
                continue
            track = project.track_by_id(part.track_id) if project and part.track_id else None
            name = track.name if track is not None else (part.media_id or "Audio")
            duration = track.original.duration_ms if track is not None else None
            chip = AudioChip(part.track_id or "", name, duration)
            chip.play_requested.connect(self.play_requested)
            chip.save_requested.connect(self.save_requested)
            layout.addWidget(chip)
        if not text and not any(part.type == "audio" for part in message.parts):
            empty = QLabel("No response text or audio.")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        footer = QHBoxLayout()
        self.regenerate_btn = QPushButton("Re-generate")
        self.regenerate_btn.clicked.connect(
            lambda: self.regenerate_requested.emit(self.message_id)
        )
        footer.addWidget(self.regenerate_btn)
        footer.addStretch()
        layout.addLayout(footer)

    def set_actions_enabled(self, enabled: bool) -> None:
        self.regenerate_btn.setEnabled(enabled)


class PromptComposer(QWidget):
    generate_requested = pyqtSignal(object)
    model_edited = pyqtSignal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._images: list[Path] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pending_attach = QFrame()
        self.pending_attach.setFrameShape(QFrame.Shape.StyledPanel)
        self.pending_attach.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        pending_layout = QVBoxLayout(self.pending_attach)
        pending_layout.addWidget(QLabel("<b>Attached</b>"))
        self.pending_list = QVBoxLayout()
        pending_layout.addLayout(self.pending_list)
        self.pending_attach.setVisible(False)
        layout.addWidget(self.pending_attach)

        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText("Describe the music you want… (Ctrl+Enter to generate)")
        self.prompt.setFixedHeight(height_for_lines(self.prompt, 4))
        self.prompt.installEventFilter(self)
        layout.addWidget(self.prompt)

        self.negative = QLineEdit()
        self.negative.setPlaceholderText("Negative prompt (optional)")
        layout.addWidget(self.negative)

        controls = QHBoxLayout()
        self.model = QLineEdit()
        self.model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.model.setMinimumWidth(0)
        self.model.setPlaceholderText(DEFAULT_COMPOSITION_MODEL)
        self.model.setText(ctx.settings.composition_model or DEFAULT_COMPOSITION_MODEL)
        self.model.editingFinished.connect(self.model_edited.emit)
        self.attach = QPushButton("Attach image")
        self.attach.clicked.connect(self._attach_image)
        self.generate = QPushButton("Generate")
        self.generate.clicked.connect(self._submit)
        controls.addWidget(QLabel("Model:"))
        controls.addWidget(self.model, 1)
        controls.addWidget(self.attach)
        controls.addWidget(self.generate)
        layout.addLayout(controls)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if watched is self.prompt and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if isinstance(key_event, QKeyEvent) and key_event.key() in (
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
            ):
                if key_event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    self._submit()
                    return True
        return super().eventFilter(watched, event)

    def _attach_image(self) -> None:
        paths, _ok = QFileDialog.getOpenFileNames(
            self,
            "Attach image",
            filter="Images (*.png *.jpg *.jpeg *.webp *.gif)",
        )
        for path in paths:
            self._images.append(Path(path))
        self._rebuild_attachments()

    def _remove_image(self, path: Path) -> None:
        try:
            self._images.remove(path)
        except ValueError:
            return
        self._rebuild_attachments()

    def _rebuild_attachments(self) -> None:
        while self.pending_list.count():
            item = self.pending_list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        if not self._images:
            self.pending_attach.setVisible(False)
            return
        for path in self._images:
            chip = AttachmentChip(path)
            chip.removed.connect(self._remove_image)
            self.pending_list.addWidget(chip)
        self.pending_attach.setVisible(True)

    def _submit(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt or not self.generate.isEnabled():
            return
        negative = self.negative.text().strip() or None
        mimes = [_IMAGE_MIMES.get(path.suffix.lower(), "image/png") for path in self._images]
        self.generate_requested.emit(
            PromptSubmission(
                prompt=prompt,
                images=list(self._images),
                image_mimes=mimes,
                model=self.current_model(),
                negative_prompt=negative,
            )
        )

    def set_busy(self, busy: bool) -> None:
        self.generate.setEnabled(not busy)
        self.generate.setText("Generating…" if busy else "Generate")
        self.prompt.setReadOnly(busy)
        self.model.setReadOnly(busy)

    def clear(self) -> None:
        self.prompt.clear()
        self.negative.clear()
        self._images.clear()
        self._rebuild_attachments()

    def current_model(self) -> str:
        return (
            self.model.text().strip()
            or self._ctx.settings.composition_model
            or DEFAULT_COMPOSITION_MODEL
        )

    def set_model(self, model: str) -> None:
        self.model.blockSignals(True)
        self.model.setText(model)
        self.model.blockSignals(False)

    def load_draft(
        self,
        *,
        prompt: str,
        images: list[Path],
        model: str,
        negative_prompt: str | None,
    ) -> None:
        self.prompt.setPlainText(prompt)
        self.negative.setText(negative_prompt or "")
        self._images = list(images)
        self._rebuild_attachments()
        if model.strip():
            self.set_model(model.strip())


class ChatWindow(QMainWindow):
    generate_requested = pyqtSignal(object)
    play_track_requested = pyqtSignal(str)
    save_track_requested = pyqtSignal(str)
    conversations_changed = pyqtSignal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._warning_texts: list[str] = []
        self._busy = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"Chat with Lyria — {APP_NAME}")
        size_chat_window(self)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)

        self.warning_host = QWidget()
        self.warning_list = QVBoxLayout(self.warning_host)
        self.warning_list.setContentsMargins(0, 0, 0, 0)
        self.warning_scroll = QScrollArea()
        self.warning_scroll.setWidgetResizable(True)
        self.warning_scroll.setWidget(self.warning_host)
        self.warning_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.warning_scroll.setMaximumHeight(height_for_lines(self.warning_scroll, 5))
        self.warning_scroll.setVisible(False)
        root_layout.addWidget(self.warning_scroll)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.chats = QListWidget()
        self.chats.setMinimumWidth(0)
        self.chats.currentItemChanged.connect(self._on_chat_selected)
        self.chats.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chats.customContextMenuRequested.connect(self._show_chat_menu)
        self.chats.installEventFilter(self)
        side_layout.addWidget(self.chats, 1)
        splitter.addWidget(sidebar)

        thread = QWidget()
        thread_layout = QVBoxLayout(thread)
        thread_layout.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel(DEFAULT_CONVERSATION_TITLE)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        thread_layout.addWidget(self.title_label)

        self.history_host = QWidget()
        self.history_list = QVBoxLayout(self.history_host)
        self.history_list.setContentsMargins(0, 0, 0, 0)
        self.history_list.addStretch()
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setWidget(self.history_host)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        thread_layout.addWidget(self.history_scroll, 1)

        self.composer = PromptComposer(ctx)
        self.composer.generate_requested.connect(self._on_composer_submit)
        self.composer.model_edited.connect(self._save_model_to_conversation)
        thread_layout.addWidget(self.composer)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setVisible(False)
        thread_layout.addWidget(self.busy)
        splitter.addWidget(thread)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([220, 640])
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self._build_menu()
        self.reload()

    def _build_menu(self) -> None:
        new_chat = QAction("&New conversation", self)
        new_chat.setShortcut("Ctrl+N")
        new_chat.triggered.connect(self._new_conversation)
        rename = QAction("&Rename…", self)
        rename.setShortcut("F2")
        rename.triggered.connect(self._rename_conversation)
        duplicate = QAction("&Duplicate", self)
        duplicate.triggered.connect(self._duplicate_conversation)
        clear = QAction("&Clear chat history", self)
        clear.triggered.connect(self._clear_history)
        delete = QAction("&Delete conversation", self)
        delete.triggered.connect(self._delete_conversation)
        menu = QMenuBar(self)
        convo = menu.addMenu("&Conversation")
        convo.addActions([new_chat, rename, duplicate, clear, delete])
        self.setMenuBar(menu)
        self._menu_actions = [new_chat, rename, duplicate, clear, delete]

    def eventFilter(self, watched, event: QEvent) -> bool:
        if watched is self.chats and event.type() == QEvent.Type.KeyPress:
            if (
                isinstance(event, QKeyEvent)
                and event.key() == Qt.Key.Key_Delete
                and not event.modifiers()
            ):
                self._delete_conversation(confirm=False)
                return True
        return super().eventFilter(watched, event)

    def selected_model(self) -> str:
        return self.composer.current_model()

    def flush_model(self) -> None:
        self._save_model_to_conversation()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy.setVisible(busy)
        self.composer.set_busy(busy)
        has_project = self._ctx.current_project is not None
        for action in self._menu_actions:
            action.setEnabled(not busy and has_project)
        self._set_history_actions_enabled(not busy)

    def clear_composer(self) -> None:
        self.composer.clear()

    def add_warning(self, text: str) -> None:
        if text in self._warning_texts:
            return
        self._warning_texts.append(text)
        self.warning_list.addWidget(WarningBubble(text))
        self.warning_scroll.setVisible(True)

    def clear_warnings(self) -> None:
        self._warning_texts.clear()
        while self.warning_list.count():
            item = self.warning_list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.warning_scroll.setVisible(False)

    def reload(self) -> None:
        project = self._ctx.current_project
        has_project = project is not None
        for action in self._menu_actions:
            action.setEnabled(has_project)
        self.chats.blockSignals(True)
        self.chats.clear()
        active_id = project.conversation_log.active_id if project is not None else None
        active_row = 0
        if project is not None:
            for index, conversation in enumerate(project.conversation_log.conversations):
                item = QListWidgetItem(conversation.display_title())
                item.setData(Qt.ItemDataRole.UserRole, conversation.id)
                item.setToolTip(conversation.display_title())
                self.chats.addItem(item)
                if conversation.id == active_id:
                    active_row = index
            if self.chats.count():
                self.chats.setCurrentRow(active_row)
        self.chats.blockSignals(False)
        name = project.name if project is not None else "No project"
        self.setWindowTitle(f"Chat with Lyria — {name}")
        self._reload_thread()

    def _set_history_actions_enabled(self, enabled: bool) -> None:
        for index in range(self.history_list.count()):
            item = self.history_list.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, (UserBubble, AssistantBubble)):
                widget.set_actions_enabled(enabled)

    def _active_conversation(self) -> Conversation | None:
        project = self._ctx.current_project
        if project is None:
            return None
        return project.conversation_by_id(project.conversation_log.active_id) or (
            project.conversation_log.conversations[0]
            if project.conversation_log.conversations
            else None
        )

    def _on_chat_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        project = self._ctx.current_project
        if project is None or current is None:
            return
        conversation_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        if not conversation_id or conversation_id == project.conversation_log.active_id:
            self._reload_thread()
            return
        self._save_model_to_conversation()
        project.set_active_conversation(conversation_id)
        self._reload_thread()

    def _reload_thread(self) -> None:
        while self.history_list.count():
            item = self.history_list.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        conversation = self._active_conversation()
        project = self._ctx.current_project
        if conversation is None:
            self.title_label.setText(DEFAULT_CONVERSATION_TITLE)
            hint = QLabel("Open or create a project, then start a conversation with Lyria.")
            hint.setWordWrap(True)
            self.history_list.addWidget(hint)
            self.history_list.addStretch()
            self._sync_model_from_conversation()
            return
        self.title_label.setText(conversation.display_title())
        if not conversation.messages:
            hint = QLabel(
                "This conversation is empty. Describe a song to generate it, then revise "
                "in follow-up messages. Use New conversation for a different song."
            )
            hint.setWordWrap(True)
            self.history_list.addWidget(hint)
        else:
            for message in conversation.messages:
                if message.role == "user":
                    paths = _image_paths_for_message(project, message)
                    bubble = UserBubble(message, paths)
                    bubble.edit_requested.connect(self._edit_message)
                    bubble.set_actions_enabled(not self._busy)
                    self.history_list.addWidget(bubble)
                else:
                    bubble = AssistantBubble(message, project)
                    bubble.play_requested.connect(self.play_track_requested)
                    bubble.save_requested.connect(self.save_track_requested)
                    bubble.regenerate_requested.connect(self._regenerate_message)
                    bubble.set_actions_enabled(not self._busy)
                    self.history_list.addWidget(bubble)
        self.history_list.addStretch()
        self._sync_model_from_conversation()
        QTimer.singleShot(0, self._scroll_history_to_bottom)

    def _scroll_history_to_bottom(self) -> None:
        bar = self.history_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_composer_submit(self, submission: PromptSubmission) -> None:
        project = self._ctx.current_project
        if project is not None:
            submission.conversation_id = project.ensure_active_conversation().id
            self._save_model_to_conversation()
        self.generate_requested.emit(submission)

    def _composition_model(self) -> str:
        return (
            self._ctx.settings.composition_model
            or DEFAULT_COMPOSITION_MODEL
        )

    def _sync_model_from_conversation(self) -> None:
        conversation = self._active_conversation()
        fallback = self._composition_model()
        if conversation is None:
            self.composer.set_model(fallback)
            return
        if conversation.model.strip():
            self.composer.set_model(conversation.model.strip())
            return
        stored = conversation.resolved_model()
        self.composer.set_model(resolve_composition_model(stored) if stored else fallback)

    def _save_model_to_conversation(self) -> None:
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None:
            return
        model = self.composer.current_model()
        if conversation.model == model:
            return
        conversation.model = model
        conversation.modified_at = utc_now()
        project.mark_dirty()
        try:
            project.save()
        except OSError:
            pass

    def _persist_chat_change(self) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        try:
            project.save()
        except OSError:
            pass
        self.conversations_changed.emit()
        self.reload()

    def _new_conversation(self) -> None:
        project = self._ctx.current_project
        if project is None:
            return
        self._save_model_to_conversation()
        active = project.ensure_active_conversation()
        if not active.messages:
            self.reload()
            self.composer.prompt.setFocus()
            return
        project.new_conversation(model=self._composition_model())
        self._persist_chat_change()
        self.composer.prompt.setFocus()

    def _duplicate_conversation(self) -> None:
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None or self._busy:
            return
        self._save_model_to_conversation()
        if project.duplicate_conversation(conversation.id) is None:
            return
        self._persist_chat_change()
        self.composer.prompt.setFocus()

    def _rename_conversation(self) -> None:
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None:
            return
        title, ok = QInputDialog.getText(
            self,
            "Rename conversation",
            "Name:",
            text=conversation.display_title(),
        )
        if not ok:
            return
        if project.rename_conversation(conversation.id, title):
            self._persist_chat_change()

    def _clear_history(self) -> None:
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None:
            return
        if conversation.messages and not ask_yes_no(
            self,
            "Clear chat history",
            "Remove all messages in this conversation? Generated tracks stay in the project.",
        ):
            return
        if project.clear_conversation(conversation.id):
            self._persist_chat_change()

    def _delete_conversation(self, *, confirm: bool = True) -> None:
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None or self._busy:
            return
        remaining = project.conversation_log.conversations
        if len(remaining) == 1 and not conversation.messages:
            return
        if confirm and not ask_yes_no(
            self,
            "Delete conversation",
            f'Delete "{conversation.display_title()}"? Generated tracks stay in the project.',
        ):
            return
        if project.delete_conversation(conversation.id, replacement_model=self._composition_model()):
            self._persist_chat_change()
            if not confirm:
                self.chats.setFocus()

    def _edit_message(self, message_id: str) -> None:
        if self._busy or not message_id:
            return
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None:
            return
        message = next((item for item in conversation.messages if item.id == message_id), None)
        if message is None or message.role != "user":
            return
        draft = self._draft_from_user_message(conversation, message)
        if not project.truncate_messages_from(conversation.id, message_id):
            return
        self._persist_chat_change()
        self.composer.load_draft(
            prompt=draft.prompt,
            images=draft.images,
            model=draft.model,
            negative_prompt=draft.negative_prompt,
        )
        self._save_model_to_conversation()
        self.composer.prompt.setFocus()

    def _regenerate_message(self, message_id: str) -> None:
        if self._busy or not message_id:
            return
        project = self._ctx.current_project
        conversation = self._active_conversation()
        if project is None or conversation is None:
            return
        message = next((item for item in conversation.messages if item.id == message_id), None)
        if message is None or message.role != "assistant":
            return
        if not project.truncate_messages_from(conversation.id, message_id):
            return
        conversation = self._active_conversation()
        last_user = next(
            (item for item in reversed(conversation.messages) if item.role == "user"),
            None,
        ) if conversation is not None else None
        self._persist_chat_change()
        if conversation is None or last_user is None:
            return
        submission = self._submission_from_user_message(conversation, last_user)
        submission.regenerate = True
        self.generate_requested.emit(submission)

    def _draft_from_user_message(
        self, conversation: Conversation, message: Message
    ) -> _UserDraft:
        generation = message.generation
        model = ""
        negative = None
        if generation is not None:
            model = (generation.model or "").strip()
            negative = generation.negative_prompt
        if not model:
            stored = conversation.resolved_model()
            model = resolve_composition_model(stored) if stored else self._composition_model()
        return _UserDraft(
            prompt=message.text().strip(),
            images=_image_paths_for_message(self._ctx.current_project, message),
            model=model,
            negative_prompt=negative,
        )

    def _submission_from_user_message(
        self, conversation: Conversation, message: Message
    ) -> PromptSubmission:
        draft = self._draft_from_user_message(conversation, message)
        mimes: list[str] = []
        for part in message.parts:
            if part.type != "image":
                continue
            mimes.append(part.mime or "image/png")
        generation = message.generation
        return PromptSubmission(
            prompt=draft.prompt,
            images=list(draft.images),
            image_mimes=mimes,
            model=draft.model,
            negative_prompt=draft.negative_prompt,
            seed=generation.seed if generation is not None else None,
            sample_count=generation.sample_count if generation is not None else 1,
            conversation_id=conversation.id,
        )

    def _show_chat_menu(self, pos) -> None:
        item = self.chats.itemAt(pos)
        if item is None:
            return
        self.chats.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Rename…", self._rename_conversation)
        menu.addAction("Duplicate", self._duplicate_conversation)
        menu.addAction("Clear history", self._clear_history)
        menu.addAction("Delete", self._delete_conversation)
        menu.exec(self.chats.mapToGlobal(pos))

    def closeEvent(self, event) -> None:
        self._save_model_to_conversation()
        super().closeEvent(event)


def _image_thumb(path: Path) -> QLabel:
    label = QLabel()
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        label.setText(path.name)
        return label
    scaled = pixmap.scaledToHeight(96, Qt.TransformationMode.SmoothTransformation)
    if scaled.width() > 160:
        scaled = pixmap.scaled(160, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    label.setPixmap(scaled)
    label.setToolTip(path.name)
    return label


def _model_id_label(generation: GenerationParams) -> QLabel:
    raw = (generation.model or "").strip()
    text = resolve_composition_model(raw) if raw else ""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    return label


def _image_paths_for_message(project: Project | None, message: Message) -> list[Path]:
    if project is None:
        return []
    paths: list[Path] = []
    for part in message.parts:
        if part.type != "image" or not part.media_id:
            continue
        found = project.find_media(part.media_id)
        if found is not None:
            paths.append(found)
    return paths
