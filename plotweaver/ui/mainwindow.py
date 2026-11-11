"""Main window: sidebar tree, editor, linked-mentions panel."""
from __future__ import annotations

import html
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl, Signal, QFileSystemWatcher
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QCompleter, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter,
                               QSpinBox, QStackedWidget, QStyle, QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..model import (CHAPTER_NUM_RE, OUTLINE_TYPES, STATUSES, TYPE_LABELS, Entity, ModelError,
                     Project, Query, as_list, clean_title, natural_key, strip_link)
from ..prose import PROSE_FILTER
from ..sample import build_sample, is_older, sample_version
from ..settings import app_settings, is_portable, sample_locations
from ..wordlog import DEFAULT_DAY_START, WordLog, draft_counts
from .editor import EntityEditor
from .theme import THEMES, accent, apply_theme, current_theme
from .graph import GraphView
from .progress import ProgressView
from .views import CorkboardView, MatrixView
from .widgets import avatar, image_size, make_completer, status_icon

KEY = Qt.UserRole
ROLE_ORDER = {"protagonist": 0, "love interest": 1, "antagonist": 2, "rival": 3}


def ekey(path) -> str:
    return "e:" + str(path)


class SideTree(QTreeWidget):
    """Sidebar tree. Outline items can be dragged; we do the move ourselves on disk."""
    moveRequested = Signal(str, object, int)  # entity path, new parent path or None, index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        hdr = self.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(True)

    @staticmethod
    def key_of(item) -> str:
        return item.data(0, KEY) if item else ""

    def _sib_keys(self, parent_item):
        return [self.key_of(parent_item.child(i)) for i in range(parent_item.childCount())]

    def dropEvent(self, event):
        src = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        pos = self.dropIndicatorPosition()
        event.ignore()  # never let Qt rearrange items itself
        if not src or not target or src is target:
            return
        skey = self.key_of(src)
        tkey = self.key_of(target)
        if not skey.startswith("e:"):
            return
        if pos == QAbstractItemView.OnItem:
            if tkey == "g:outline":
                parent_key = None
                index = len([k for k in self._sib_keys(target) if k != skey])
            elif target.data(0, KEY + 1) in ("part", "chapter"):
                parent_key = tkey
                index = len([k for k in self._sib_keys(target) if k != skey])
            else:  # dropped onto a scene: go just after it
                parent_item = target.parent()
                parent_key = None if self.key_of(parent_item) == "g:outline" else self.key_of(parent_item)
                sibs = [k for k in self._sib_keys(parent_item) if k != skey]
                index = sibs.index(tkey) + 1
        elif pos in (QAbstractItemView.AboveItem, QAbstractItemView.BelowItem):
            parent_item = target.parent()
            if parent_item is None:
                return
            pk = self.key_of(parent_item)
            if not (pk == "g:outline" or pk.startswith("e:")):
                return
            parent_key = None if pk == "g:outline" else pk
            sibs = [k for k in self._sib_keys(parent_item) if k != skey]
            if tkey not in sibs:
                return
            index = sibs.index(tkey) + (1 if pos == QAbstractItemView.BelowItem else 0)
        else:
            return
        parent_path = parent_key[2:] if parent_key else None
        QTimer.singleShot(0, lambda: self.moveRequested.emit(skey[2:], parent_path, index))


class TagCompleter(QCompleter):
    """Completes only the #tag being typed at the end of a filter line."""

    def splitPath(self, path):
        last = path.split(" ")[-1] if path else ""
        return [last] if last.startswith("#") else ["\x00"]

    def pathFromIndex(self, index):
        text = super().pathFromIndex(index)
        parts = self.widget().text().split(" ")
        return " ".join(parts[:-1] + [text]) + " "


class QuickOpen(QDialog):
    def __init__(self, names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Go to…")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        self.line = QLineEdit()
        self.line.setPlaceholderText("Type a name…")
        c = make_completer(self.line)
        c.model().setStringList(names)
        self.line.setCompleter(c)
        c.activated.connect(lambda *_: QTimer.singleShot(0, self.accept))
        lay.addWidget(self.line)
        self.line.returnPressed.connect(self.accept)


class NewChapterDialog(QDialog):
    """A chapter always starts with one scene, so ask for both names at once."""

    def __init__(self, project: Project, default_title: str, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("New chapter")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit(default_title)
        self.scene = QLineEdit()
        self.scene.setPlaceholderText("e.g. Hearthlight  (blank = “Untitled scene”)")
        form.addRow("Chapter", self.title)
        form.addRow("First scene", self.scene)
        lay.addLayout(form)
        self.renumber = QCheckBox()
        self.renumber.setChecked(True)
        lay.addWidget(self.renumber)
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {accent('error')};")
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok = bb.button(QDialogButtonBox.Ok)
        lay.addWidget(bb)
        self.title.textChanged.connect(self._check)
        self.scene.textChanged.connect(self._check)
        self._check()
        numbered = bool(CHAPTER_NUM_RE.match(default_title))
        (self.scene if numbered else self.title).setFocus()
        if not numbered:
            self.title.selectAll()

    def _check(self):
        t = clean_title(self.title.text())
        sc = clean_title(self.scene.text())
        taken = bool(t) and self.project.name_taken(t)
        can_shift = taken and bool(CHAPTER_NUM_RE.match(t)) and \
            isinstance(self.project.resolve(t), Entity) and self.project.resolve(t).type == "chapter"
        self.renumber.setVisible(can_shift)
        self.renumber.setText(f"“{t}” exists — renumber the chapters after it to make room")
        err = ""
        if not t:
            err = "The chapter needs a name."
        elif taken and not (can_shift and self.renumber.isChecked()):
            err = f"Something is already named “{t}”."
        elif sc and sc.lower() == t.lower():
            err = "The scene needs a different name from its chapter."
        elif sc and self.project.name_taken(sc):
            err = f"Something is already named “{sc}”."
        self.error.setText(err)
        self.ok.setEnabled(not err)


class Preferences(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        row = QHBoxLayout()
        self.cmd = QLineEdit(settings.value("editor_cmd", "", str))
        self.cmd.setPlaceholderText(f"Auto-detect FocusWriter ({detect_focuswriter() or 'not found'}), "
                                    "else system default")
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(self.cmd, 1)
        row.addWidget(b)
        form.addRow("Draft editor command", row)
        hint = QLabel("Leave blank to auto-detect. Examples: <code>C:\\Program Files (x86)\\FocusWriter\\"
                      "FocusWriter.exe</code> · <code>flatpak run org.gottcode.FocusWriter</code>")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        form.addRow("", hint)
        self.day_start = QSpinBox()
        self.day_start.setRange(0, 12)
        self.day_start.setSuffix(":00")
        self.day_start.setValue(int(settings.value("day_start_hour", DEFAULT_DAY_START)))
        self.day_start.setToolTip("Words written before this hour count toward the previous day, "
                                  "so a late-night session stays one day.")
        form.addRow("New writing day starts at", self.day_start)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose your writing app")
        if f:
            self.cmd.setText(f)

    def accept(self):
        self.settings.setValue("editor_cmd", self.cmd.text().strip())
        self.settings.setValue("day_start_hour", self.day_start.value())
        super().accept()


def detect_focuswriter() -> str:
    for name in ("focuswriter", "FocusWriter"):
        p = shutil.which(name)
        if p:
            return p
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            for sub in ("FocusWriter", os.path.join("Programs", "FocusWriter")):
                cand = Path(base) / sub / "FocusWriter.exe"
                if cand.exists():
                    return str(cand)
    return ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = app_settings()
        self.project: Project | None = None
        self._first_build = True
        self.setWindowTitle("Plotweaver")
        self.resize(1280, 820)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._fs_changed)
        self.watcher.fileChanged.connect(self._fs_changed)
        self.fs_timer = QTimer(self, singleShot=True, interval=400)
        self.fs_timer.timeout.connect(self._handle_fs)

        # ---------- sidebar
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 0, 6)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter…  (names, or #tag)")
        self.filter.setToolTip("Filter by name or summary text, and/or #tags.\n"
                               "Several terms must all match: “#flashback sera”.\n"
                               "The corkboard, matrix and relationship scene counts follow this filter too.")
        self.filter.setClearButtonEnabled(True)
        self.filter_completer = make_completer(self.filter, TagCompleter)
        self.filter.setCompleter(self.filter_completer)
        self.filter.textChanged.connect(self._filter_changed)
        self.tree = SideTree()
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.moveRequested.connect(self._do_move)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        sl.addWidget(self.filter)
        sl.addWidget(self.tree, 1)

        # ---------- editor
        self.editor = EntityEditor()
        self.editor.saved.connect(self._on_saved)
        self.editor.renameRequested.connect(lambda p, t: QTimer.singleShot(0, lambda: self._do_rename(p, t)))
        self.editor.followLink.connect(self.follow_link)
        self.editor.openProse.connect(self.open_prose_path)
        self.editor.browseProse.connect(self.browse_prose)
        self.editor.createProse.connect(self.create_prose)
        self.editor.error.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.editor.openFile.connect(lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))

        # ---------- views
        self.cork = CorkboardView(self.settings)
        self.matrix = MatrixView(self.settings)
        self.graph = GraphView(self.settings)
        for v in (self.cork, self.matrix, self.graph):
            v.selected.connect(self._view_selected)
            v.opened.connect(self._view_opened)
        self.cork.moveRequested.connect(self._do_move)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.editor, "Editor")
        self.tabs.addTab(self.cork, "Corkboard")
        self.tabs.addTab(self.matrix, "Matrix")
        self.tabs.addTab(self.graph, "Relationships")
        self.wordlog: WordLog | None = None
        self.progress = ProgressView(self.settings)
        self.progress.changed.connect(self._update_stats)
        self.tabs.addTab(self.progress, "Progress")
        self.tabs.currentChanged.connect(lambda *_: self._refresh_views())

        # ---------- linked mentions
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 6, 6, 6)
        self.back_label = QLabel("<b>Linked mentions</b>")
        self.backlinks = QListWidget()
        self.backlinks.itemActivated.connect(self._open_list_item)
        self.unres_label = QLabel("<b>Unresolved links</b>")
        self.unresolved = QListWidget()
        self.unresolved.setToolTip("Links to notes that don't exist yet. Double-click to create one.")
        self.unresolved.itemActivated.connect(self._create_unresolved)
        self.cast_label = QLabel("<b>In this scene</b>")
        self.cast = QListWidget()
        self.cast.setWordWrap(True)
        self.cast.setSpacing(3)
        self.cast.setTextElideMode(Qt.ElideNone)
        self.cast.setIconSize(QSize(44, 44))
        self.cast.setToolTip("POV and characters in this scene. Double-click to open.")
        self.cast.itemActivated.connect(self._open_list_item)
        rl.addWidget(self.cast_label)
        rl.addWidget(self.cast, 2)
        rl.addWidget(self.back_label)
        rl.addWidget(self.backlinks, 3)
        rl.addWidget(self.unres_label)
        rl.addWidget(self.unresolved, 1)

        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(side)
        self.split.addWidget(self.tabs)
        self.split.addWidget(right)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([300, 700, 260])

        # ---------- welcome
        welcome = QWidget()
        wl = QVBoxLayout(welcome)
        wl.addStretch(1)
        t = QLabel("Plotweaver")
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 2.4)
        f.setBold(True)
        t.setFont(f)
        t.setAlignment(Qt.AlignCenter)
        sub = QLabel("A novel outliner that keeps everything in plain Markdown.")
        sub.setAlignment(Qt.AlignCenter)
        wl.addWidget(t)
        wl.addWidget(sub)
        brow = QHBoxLayout()
        brow.addStretch(1)
        b_new, b_open = QPushButton("New project…"), QPushButton("Open project…")
        b_sample = QPushButton("Open the sample project")
        b_sample.setToolTip("“The Ember Crown”: a small example novel that uses every feature.")
        b_new.clicked.connect(self.new_project)
        b_open.clicked.connect(self.open_project_dialog)
        b_sample.clicked.connect(self.open_sample)
        brow.addWidget(b_new)
        brow.addWidget(b_open)
        brow.addWidget(b_sample)
        brow.addStretch(1)
        wl.addLayout(brow)
        wl.addStretch(2)

        self.stack = QStackedWidget()
        self.stack.addWidget(welcome)
        self.stack.addWidget(self.split)
        self.setCentralWidget(self.stack)

        self.stats_label = QLabel()
        self.statusBar().addPermanentWidget(self.stats_label)
        # keep "Today" right when the app sits open across the start of a new day
        self.day_timer = QTimer(self, interval=5 * 60 * 1000)
        self.day_timer.timeout.connect(self._update_stats)
        self.day_timer.start()

        self._build_actions()
        QApplication.instance().applicationStateChanged.connect(self._app_state)
        self._set_project_actions(False)

        last = self.settings.value("last_project", "", str)
        if last and Project.is_project(last):
            QTimer.singleShot(0, lambda: self.open_project(last))

    # ================================================================ actions
    def _act(self, text, slot, shortcut=None, tip=None, icon=None, context=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if context is not None:
            a.setShortcutContext(context)
        a.triggered.connect(lambda *_: slot())
        return a

    def _build_actions(self):
        S = QStyle
        self.a_new_proj = self._act("&New Project…", self.new_project, "Ctrl+Shift+N")
        self.a_open_sample = self._act("Open &Sample Project", self.open_sample,
                                       tip="A small example novel that uses every feature")
        self.a_open_proj = self._act("&Open Project…", self.open_project_dialog, "Ctrl+O",
                                     icon=S.SP_DialogOpenButton)
        self.a_reveal = self._act("Show Project &Folder", self.reveal_folder)
        self.a_reload = self._act("&Reload from Disk", self.reload, "F5", icon=S.SP_BrowserReload)
        self.a_prefs = self._act("&Preferences…", self.preferences)
        self.a_quit = self._act("&Quit", self.close, "Ctrl+Q")

        self.a_part = self._act("New &Part", lambda: self.new_item("part"), tip="New part")
        self.a_chapter = self._act("New &Chapter", lambda: self.new_item("chapter"), "Ctrl+Shift+C",
                                   tip="New chapter")
        self.a_scene = self._act("New &Scene", lambda: self.new_item("scene"), "Ctrl+N", tip="New scene")
        self.a_char = self._act("New C&haracter", lambda: self.new_item("character"), "Ctrl+Shift+H")
        self.a_place = self._act("New P&lace", lambda: self.new_item("place"), "Ctrl+Shift+L")
        self.a_codex = self._act("New Code&x Entry", lambda: self.new_item("codex"), "Ctrl+Shift+X")
        self.a_note = self._act("New &Note", lambda: self.new_item("note"))

        self.a_up = self._act("Move &Up", lambda: self.move_step(-1), "Alt+Up", icon=S.SP_ArrowUp)
        self.a_down = self._act("Move &Down", lambda: self.move_step(1), "Alt+Down", icon=S.SP_ArrowDown)
        self.a_rename = self._act("&Rename", self.rename_current, "F2")
        self.a_renumber = self._act("Re&number Chapters", self.renumber_chapters,
                                    tip="Rename “Chapter N” chapters so N matches their order")
        self.a_delete = self._act("&Delete", self.delete_current, "Delete", icon=S.SP_TrashIcon,
                                  context=Qt.WidgetShortcut)
        self.tree.addAction(self.a_delete)
        self.a_import = self._act("&Import Drafts as Scenes…", self.import_drafts)
        self.a_open_draft = self._act("Open &Draft", self.open_prose_current, "Ctrl+E",
                                      tip="Open this scene's draft in your writing app")
        self.a_goto = self._act("&Go to…", self.quick_open, "Ctrl+P")
        self.a_tab_editor = self._act("&Editor", lambda: self.tabs.setCurrentIndex(0), "Ctrl+1")
        self.a_tab_cork = self._act("&Corkboard", lambda: self.tabs.setCurrentIndex(1), "Ctrl+2")
        self.a_tab_matrix = self._act("&Matrix", lambda: self.tabs.setCurrentIndex(2), "Ctrl+3")
        self.a_tab_graph = self._act("&Relationships", lambda: self.tabs.setCurrentIndex(3), "Ctrl+4")
        self.a_tab_progress = self._act("&Progress", lambda: self.tabs.setCurrentIndex(4), "Ctrl+5",
                                        tip="Words written per day")
        self.a_find_tag = self._act("Filter by &Tag…", self._focus_tag_filter, "Ctrl+T")

        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.a_new_proj, self.a_open_proj])
        self.recent_menu = m.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addAction(self.a_open_sample)
        m.addSeparator()
        m.addActions([self.a_reveal, self.a_reload, self.a_import])
        m.addSeparator()
        m.addActions([self.a_prefs, self.a_quit])
        m = mb.addMenu("&New")
        m.addActions([self.a_part, self.a_chapter, self.a_scene])
        m.addSeparator()
        m.addActions([self.a_char, self.a_place, self.a_codex, self.a_note])
        m = mb.addMenu("&Outline")
        m.addActions([self.a_up, self.a_down, self.a_rename, self.a_delete])
        m.addAction(self.a_renumber)
        m.addSeparator()
        m.addActions([self.a_open_draft, self.a_goto])
        m = mb.addMenu("&View")
        m.addActions([self.a_tab_editor, self.a_tab_cork, self.a_tab_matrix, self.a_tab_graph,
                      self.a_tab_progress])
        m.addSeparator()
        tm = m.addMenu("T&heme")
        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for t in THEMES:
            a = tm.addAction(t.capitalize())
            a.setCheckable(True)
            a.setChecked(t == current_theme())
            a.triggered.connect(lambda *_, t=t: self.set_theme(t))
            self.theme_group.addAction(a)
            self.theme_actions[t] = a
        m.addSeparator()
        m.addAction(self.a_find_tag)
        m = mb.addMenu("&Help")
        m.addAction(self._act("&About", self.about))

        tb = QToolBar("Main")
        tb.setObjectName("main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for a in (self.a_part, self.a_chapter, self.a_scene):
            tb.addAction(a)
        tb.addSeparator()
        for a, label in ((self.a_char, "Character"), (self.a_place, "Place"), (self.a_codex, "Codex"),
                         (self.a_note, "Note")):
            b = tb.addAction("+ " + label)
            b.triggered.connect(a.trigger)
            b.setToolTip(a.text().replace("&", ""))
            self._project_tb = getattr(self, "_project_tb", []) + [b]
        tb.addSeparator()
        tb.addAction(self.a_up)
        tb.addAction(self.a_down)
        tb.addAction(self.a_open_draft)
        tb.addAction(self.a_goto)
        self.addToolBar(tb)
        self.toolbar = tb

    def _set_project_actions(self, on: bool):
        for a in (self.a_reveal, self.a_reload, self.a_part, self.a_chapter, self.a_scene, self.a_char,
                  self.a_place, self.a_codex, self.a_note, self.a_up, self.a_down, self.a_rename,
                  self.a_delete, self.a_renumber, self.a_import, self.a_open_draft, self.a_goto, self.a_tab_editor,
                  self.a_tab_cork, self.a_tab_matrix, self.a_tab_graph, self.a_tab_progress, self.a_find_tag, *self._project_tb):
            a.setEnabled(on)
        self.toolbar.setVisible(on)

    # ================================================================ project
    def new_project(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose or create an empty folder for the new project")
        if not folder:
            return
        if Project.is_project(folder):
            self.open_project(folder)
            return
        title, ok = QInputDialog.getText(self, "New project", "Novel title:", text=Path(folder).name)
        if not ok:
            return
        try:
            Project.new_project(folder, title.strip() or Path(folder).name)
        except OSError as ex:
            QMessageBox.warning(self, "Couldn't create project", str(ex))
            return
        self.open_project(folder)

    def open_project_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Open project folder")
        if not folder:
            return
        if not Project.is_project(folder):
            r = QMessageBox.question(
                self, "Not a Plotweaver project",
                "This folder has no project.md.\n\nSet it up as a project? Existing Markdown files "
                "are left as they are.")
            if r != QMessageBox.Yes:
                return
            Project.new_project(folder, Path(folder).name)
        self.open_project(folder)

    def open_sample(self):
        """Open the sample project: the copy beside the program (portable), else the one in
        Documents/Plotweaver, building it there if it's missing and offering to refresh an
        older one."""
        self.editor.flush()
        beside, docs = sample_locations()
        if sample_version(beside) is not None:
            self.open_project(beside)
            return
        target = docs
        v = sample_version(target)
        try:
            if not target.exists():
                self.statusBar().showMessage("Creating the sample project…")
                build_sample(target)
                self.statusBar().showMessage(f"Sample project created in {target}", 8000)
            elif v is not None and is_older(v) and self.settings.value("sample/kept", "", str) != __version__:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Question)
                box.setWindowTitle("Newer sample project")
                box.setText(f"Your sample project is from Plotweaver {v}. This version ({__version__}) "
                            "has a newer one that shows off more features.")
                box.setInformativeText(f"Replace it? Any changes you made to the sample in\n{target}\nwill be lost.")
                rb = box.addButton("Replace it", QMessageBox.AcceptRole)
                box.addButton("Keep mine", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is rb:
                    if self.project and self.project.root == target.resolve():
                        self.project = None
                    build_sample(target, replace=True)
                else:
                    self.settings.setValue("sample/kept", __version__)
            elif v is None and not Project.is_project(target):
                QMessageBox.warning(self, "Sample project",
                                    f"{target} already exists but isn't a Plotweaver project, so it "
                                    "was left alone. Rename or move it and try again.")
                return
        except OSError as ex:
            QMessageBox.warning(self, "Couldn't create the sample project", str(ex))
            return
        self.open_project(target)

    def open_project(self, folder):
        self.editor.flush()
        try:
            self.project = Project(folder)
        except OSError as ex:
            QMessageBox.warning(self, "Couldn't open project", str(ex))
            return
        self.settings.setValue("last_project", str(self.project.root))
        recent = [r for r in self.settings.value("recent", [], list) or [] if r != str(self.project.root)]
        self.settings.setValue("recent", [str(self.project.root)] + recent[:7])
        self.editor.set_project(self.project)
        self.cork.set_project(self.project)
        self.matrix.set_project(self.project)
        self.graph.set_project(self.project)
        self.wordlog = WordLog(self.project.root, self._day_start())
        self.progress.set_project(self.project, self.wordlog, self._daily_goal())
        self._first_build = True
        self.rebuild_tree()
        self._rewatch()
        self.stack.setCurrentIndex(1)
        self._set_project_actions(True)
        self._update_title()
        self._update_stats()
        pe = self.project.project_entity
        if pe:
            self.select(pe.path)

    def _fill_recent(self):
        self.recent_menu.clear()
        for r in self.settings.value("recent", [], list) or []:
            a = self.recent_menu.addAction(r)
            a.setEnabled(Project.is_project(r))
            a.triggered.connect(lambda *_, r=r: self.open_project(r))
        if self.recent_menu.isEmpty():
            self.recent_menu.addAction("(none)").setEnabled(False)

    def _update_title(self):
        if self.project:
            pe = self.project.project_entity
            self.setWindowTitle(f"{pe.title if pe else self.project.root.name} — Plotweaver")

    def reveal_folder(self):
        if self.project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.root)))

    def reload(self):
        if not self.project:
            return
        self.editor.flush()
        cur = self.editor.path
        self.project.load()
        self.rebuild_tree()
        self._rewatch()
        if cur and self.project.get(cur):
            self.editor.load(cur)
            self.select(cur)
        else:
            self.editor.clear()
        self._update_stats()

    # ============================================================ file watch
    def _rewatch(self):
        old = self.watcher.directories() + self.watcher.files()
        if old:
            self.watcher.removePaths(old)
        if self.project:
            paths = self.project.watch_dirs() + self.project.prose_files()
            if paths:
                self.watcher.addPaths(paths)

    def _fs_changed(self, *_):
        self.fs_timer.start()

    def _handle_fs(self):
        if not self.project:
            return
        changed = self.project.rescan()
        if changed:
            cur = self.editor.path
            self.rebuild_tree()
            if cur in changed:
                if not cur.exists():
                    self.editor.clear()
                elif not self.editor.dirty:
                    self.editor.load(cur)
            else:
                self.editor.refresh_options()
            self._update_backlinks()
        self.editor.refresh_prose()
        self._update_word_counts()
        self._update_stats()
        self._rewatch()
        self._refresh_views()

    def _app_state(self, state):
        if state == Qt.ApplicationActive and self.project:
            self._handle_fs()

    # ================================================================== tree
    def _expanded(self) -> set[str]:
        out = set()

        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                if c.isExpanded():
                    out.add(SideTree.key_of(c))
                walk(c)
        walk(self.tree.invisibleRootItem())
        return out

    def _item(self, parent, text, key, etype=None, icon=None, bold=False, tip=None):
        it = QTreeWidgetItem(parent, [text])
        it.setData(0, KEY, key)
        it.setData(0, KEY + 1, etype)
        if icon is not None:
            it.setIcon(0, icon)
        if bold:
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
        if tip:
            it.setToolTip(0, tip)
        drag = etype in OUTLINE_TYPES
        drop = etype in ("part", "chapter", "scene") or key == "g:outline"
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if drag:
            flags |= Qt.ItemIsDragEnabled
        if drop:
            flags |= Qt.ItemIsDropEnabled
        it.setFlags(flags)
        return it

    def _entity_item(self, parent, e: Entity):
        p = self.project
        tip = None
        icon = None
        text = e.name
        if e.type == "scene":
            icon = status_icon(e.status)
            tip = self._entity_item_tip(e)
        elif e.is_container:
            icon = self.style().standardIcon(QStyle.SP_DirIcon)
            words = p.words_under(e)
            n = sum(1 for _ in self._iter_scenes(e))
            tip = f"{n} scene{'s' if n != 1 else ''}" + (f" · {words:,} words" if words else "")
        elif e.type == "character":
            tip = self._character_tip(e) or None
        return self._item(parent, text, ekey(e.path), e.type, icon=icon, bold=e.type == "part", tip=tip)

    def _tree_shape(self):
        p = self.project
        return tuple(sorted((str(e.path), str(e.meta.get("role", "")), str(p.category_of(e)),
                             str(e.meta.get("parent", "")))
                            for e in p.entities.values() if e.type in ("character", "place", "codex")))

    def _entity_item_tip(self, e):
        bits = [e.status or "idea"]
        w = self.project.prose_words(e)
        if w is not None:
            bits.append(f"{w:,} words")
        tip = " · ".join(bits)
        if e.meta.get("summary"):
            tip += "\n" + str(e.meta.get("summary"))
        if e.tags:
            tip += "\n" + " ".join("#" + t for t in e.tags)
        return tip

    def _iter_scenes(self, e):
        for c in e.children:
            if c.type == "scene":
                yield c
            yield from self._iter_scenes(c)

    def rebuild_tree(self):
        p = self.project
        if not p:
            return
        expanded = self._expanded()
        cur_key = SideTree.key_of(self.tree.currentItem())
        self.tree.blockSignals(True)
        self.tree.clear()
        pe = p.project_entity
        if pe:
            self._item(self.tree, pe.title, ekey(pe.path), "project", bold=True,
                       icon=self.style().standardIcon(QStyle.SP_FileDialogDetailedView))

        g = self._item(self.tree, "Outline", "g:outline", bold=True)

        def add_outline(parent, items):
            for e in items:
                it = self._entity_item(parent, e)
                add_outline(it, e.children)
        add_outline(g, p.outline_roots())

        g = self._item(self.tree, "Characters", "g:character", bold=True)
        chars = sorted(p.by_type("character"),
                       key=lambda e: (ROLE_ORDER.get(str(e.meta.get("role", "")).lower(), 9),
                                      natural_key(e.name)))
        for e in chars:
            self._entity_item(g, e)

        g = self._item(self.tree, "Places", "g:place", bold=True)
        places = p.by_type("place")
        kids: dict = {}
        roots = []
        for e in places:
            par = p.resolve(e.meta.get("parent")) if e.meta.get("parent") else None
            # avoid cycles: only nest if the chain upward doesn't come back to e
            seen, x = {e.path}, par
            while x is not None and x.type == "place" and x.path not in seen:
                seen.add(x.path)
                x = p.resolve(x.meta.get("parent")) if x.meta.get("parent") else None
            if par is not None and par.type == "place" and x is None:
                kids.setdefault(par.path, []).append(e)
            else:
                roots.append(e)

        def add_places(parent, items):
            for e in items:
                it = self._entity_item(parent, e)
                add_places(it, kids.get(e.path, []))
        add_places(g, roots)

        g = self._item(self.tree, "Codex", "g:codex", bold=True)
        cats: dict[str, list] = {}
        for e in p.by_type("codex"):
            cats.setdefault(p.category_of(e), []).append(e)
        for cat in sorted(cats, key=natural_key):
            cg = self._item(g, cat.capitalize() if cat.islower() else cat, "g:codex:" + cat)
            for e in cats[cat]:
                self._entity_item(cg, e)

        g = self._item(self.tree, "Notes", "g:note", bold=True)
        for e in p.by_type("note"):
            self._entity_item(g, e)

        # restore state
        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                k = SideTree.key_of(c)
                if self._first_build:
                    c.setExpanded(k.startswith("g:") or c.data(0, KEY + 1) in ("part", "chapter"))
                else:
                    c.setExpanded(k in expanded)
                if k == cur_key:
                    self.tree.setCurrentItem(c)
                walk(c)
        walk(self.tree.invisibleRootItem())
        self._first_build = False
        self._shape = self._tree_shape()
        self._update_word_counts()
        self.tree.blockSignals(False)
        self.filter_completer.model().setStringList(["#" + t for t in p.all_tags()])
        if self.filter.text():
            self._apply_filter()
        self._refresh_views()

    def _update_word_counts(self):
        """Draft word counts in the sidebar's second column: per scene, totals per chapter/part."""
        if not self.project:
            return
        p = self.project
        muted_brush = self.tree.palette().placeholderText()

        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                k = SideTree.key_of(c)
                text = ""
                if k == "g:outline":
                    total = sum(p.words_under(e) for e in p.outline_roots())
                    text = f"{total:,}" if total else ""
                elif k.startswith("e:") and c.data(0, KEY + 1) in OUTLINE_TYPES:
                    e = p.get(Path(k[2:]))
                    if e is not None:
                        n = p.prose_words(e) if e.type == "scene" else p.words_under(e)
                        text = f"{n:,}" if n else ""
                if c.text(1) != text:
                    c.setText(1, text)
                    c.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                    c.setForeground(1, muted_brush)
                walk(c)
        walk(self.tree.invisibleRootItem())

    def find_item(self, path):
        key = ekey(path)

        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                if SideTree.key_of(c) == key:
                    return c
                r = walk(c)
                if r:
                    return r
            return None
        return walk(self.tree.invisibleRootItem())

    def select(self, path):
        it = self.find_item(path)
        if it:
            p = it.parent()
            while p:
                p.setExpanded(True)
                p = p.parent()
            self.tree.setCurrentItem(it)
            self.tree.scrollToItem(it)
            if self.editor.path != Path(path):
                self.editor.load(path)
            self._update_backlinks()

    def _filter_changed(self, text):
        self._apply_filter()
        q = self.query()
        self.cork.set_query(q)
        self.matrix.set_query(q)
        self.graph.set_query(q)
        self._refresh_views()

    def _refresh_views(self):
        """Rebuild the corkboard/matrix, but only the one on screen (others when shown)."""
        if not self.project:
            return
        if not hasattr(self, "tabs"):
            return
        w = self.tabs.currentWidget()
        if w is self.editor:
            return
        w.refresh()
        if w in (self.cork, self.graph):
            cur = self.current_entity()
            w.set_current(cur.path if cur else None)

    def _view_selected(self, path):
        self.editor.flush()
        self.select(path)

    def _view_opened(self, path):
        self.editor.flush()
        self.select(path)
        self.tabs.setCurrentWidget(self.editor)

    def query(self) -> Query:
        return Query(self.filter.text())

    def _apply_filter(self, text=None):
        q = self.query()

        def walk(item) -> bool:
            any_vis = False
            for i in range(item.childCount()):
                c = item.child(i)
                child_vis = walk(c)
                k = SideTree.key_of(c)
                e = self.project.get(Path(k[2:])) if self.project and k.startswith("e:") else None
                me = not q or (e is not None and q.matches(e))
                vis = me or child_vis
                c.setHidden(not vis)
                if q and child_vis:
                    c.setExpanded(True)
                any_vis = any_vis or vis
            return any_vis
        walk(self.tree.invisibleRootItem())

    def current_entity(self) -> Entity | None:
        k = SideTree.key_of(self.tree.currentItem())
        if self.project and k.startswith("e:"):
            return self.project.get(Path(k[2:]))
        return None

    def _on_current(self, cur, prev):
        self.editor.flush()
        e = self.current_entity()
        self.cork.set_current(e.path if e else None)
        self.graph.set_current(e.path if e else None)
        if e is None:
            self.editor.clear()
        elif self.editor.path != e.path:
            self.editor.load(e.path)
        self._update_backlinks()

    def _on_saved(self, path):
        e = self.project.get(path)
        it = self.find_item(path)
        if e and it:
            if e.type == "scene":
                it.setIcon(0, status_icon(e.status))
            if e.type == "project":
                it.setText(0, e.title)
                self._update_title()
            if e.type == "scene":
                it.setToolTip(0, self._entity_item_tip(e))
            elif e.type == "character":
                it.setToolTip(0, self._character_tip(e))
        # a role, category or "located in" change reshapes the tree
        if self._tree_shape() != getattr(self, "_shape", None):
            QTimer.singleShot(0, self.rebuild_tree)
        self.editor.refresh_options()
        self._update_backlinks()
        self._update_word_counts()
        self._update_stats()
        self._refresh_views()

    # =========================================================== linked panel
    @staticmethod
    def _traits(e: Entity) -> list[str]:
        return [str(t) for t in as_list(e.meta.get("traits"))]

    def _character_tip(self, e: Entity) -> str:
        """Plain text, or HTML with the portrait when there is one."""
        bits = []
        img = self.project.image_path(e.meta.get("portrait")) if e.meta.get("portrait") else None
        if e.meta.get("role"):
            bits.append(str(e.meta.get("role")))
        if self._traits(e):
            bits.append(" · ".join(self._traits(e)))
        if e.meta.get("appearance"):
            bits.append(str(e.meta.get("appearance")))
        if img is None:
            return "\n".join(bits)
        sz = image_size(img)
        w = 160
        h = int(sz.height() * w / sz.width()) if sz.isValid() and sz.width() else 200
        text = "<br>".join(html.escape(b).replace("\n", "<br>") for b in bits)
        return (f"<table><tr><td><img src='{QUrl.fromLocalFile(str(img)).toString()}' width={w} height={h}></td>"
                f"<td style='padding-left:8px; vertical-align:top'><b>{html.escape(e.name)}</b><br>{text}</td>"
                "</tr></table>")

    def _update_cast(self, e: Entity | None):
        self.cast.clear()
        show = e is not None and e.type == "scene"
        self.cast_label.setVisible(show)
        self.cast.setVisible(show)
        if not show:
            return
        names = []
        if e.meta.get("pov"):
            names.append(strip_link(e.meta.get("pov")))
        names += [strip_link(n) for n in as_list(e.meta.get("characters"))]
        seen = set()
        pov = strip_link(e.meta.get("pov")) if e.meta.get("pov") else None
        for n in names:
            c = self.project.resolve(n)
            key = c.path if c else n.lower()
            if key in seen:
                continue
            seen.add(key)
            head = c.name if c else n
            if pov and n == pov:
                head += "  (POV)"
            if c is None:
                it = QListWidgetItem(f"{head}\nno note yet")
                it.setIcon(QIcon(avatar(n, None, 44)))
                it.setForeground(Qt.gray)
                self.cast.addItem(it)
                continue
            if c.meta.get("role"):
                head += f" — {c.meta.get('role')}"
            traits = self._traits(c)
            text = head + ("\n" + " · ".join(traits) if traits else "")
            it = QListWidgetItem(text)
            it.setData(KEY, str(c.path))
            img = self.project.image_path(c.meta.get("portrait")) if c.meta.get("portrait") else None
            it.setIcon(QIcon(avatar(c.name, img, 44)))
            tip = self._character_tip(c)
            if tip:
                it.setToolTip(tip)
            self.cast.addItem(it)
        self.cast_label.setText(f"<b>In this scene</b> ({self.cast.count()})")

    def _update_backlinks(self):
        self.backlinks.clear()
        self.unresolved.clear()
        e = self.project.get(self.editor.path) if self.project and self.editor.path else None
        self._update_cast(e)
        if not e:
            self.back_label.setText("<b>Linked mentions</b>")
            return
        hits = self.project.backlinks(e)
        self.back_label.setText(f"<b>Linked mentions</b> ({len(hits)})")
        for h in hits:
            label = h.name
            if h.type == "scene":
                ch = self.project.chapter_of(h.parent) if h.parent else None
                if ch:
                    label += f"  — {ch.name}"
            it = QListWidgetItem(f"{TYPE_LABELS.get(h.type, h.type)}: {label}")
            it.setData(KEY, str(h.path))
            if h.type == "scene":
                it.setIcon(status_icon(h.status))
            self.backlinks.addItem(it)
        un = self.project.unresolved(e)
        self.unres_label.setText(f"<b>Unresolved links</b> ({len(un)})")
        for name in un:
            it = QListWidgetItem(name)
            it.setData(KEY, name)
            self.unresolved.addItem(it)

    def _open_list_item(self, item):
        if item.data(KEY):
            self.select(Path(item.data(KEY)))

    def _create_unresolved(self, item):
        name = item.data(KEY)
        types = ["character", "place", "codex", "note"]
        labels = [TYPE_LABELS[t] for t in types]
        choice, ok = QInputDialog.getItem(self, "Create note", f"Create “{name}” as a…", labels, 0, False)
        if ok:
            self._create(types[labels.index(choice)], name)

    def follow_link(self, name):
        if not self.project:
            return
        target = self.project.resolve(name)
        if target:
            self.editor.flush()
            self.select(target.path)
        else:
            self.statusBar().showMessage(f"No note named “{strip_link(name)}” yet — see Unresolved links.", 5000)

    def quick_open(self):
        if not self.project:
            return
        d = QuickOpen(self.project.names_for(), self)
        if d.exec() and d.line.text().strip():
            self.follow_link(d.line.text())

    # ================================================================ editing
    def _outline_target(self, etype):
        """Work out (parent, after) for a new outline item from the selection."""
        e = self.current_entity()
        p = self.project
        if etype == "part":
            top = e
            while top is not None and top.parent is not None:
                top = top.parent
            return None, top if top is not None and top.type in OUTLINE_TYPES else None
        if etype == "chapter":
            if e is None or e.type not in OUTLINE_TYPES:
                return None, None
            if e.type == "part":
                return e, None
            ch = p.chapter_of(e)
            return ch.parent, ch
        # scene
        if e is None or e.type not in OUTLINE_TYPES:
            chapters = [c for c in p.by_type("chapter")]
            if not chapters:
                return "need-chapter", None
            last = max(chapters, key=lambda c: p.outline_pos.get(c.path, 0))
            return last, None
        if e.type == "scene":
            return e.parent, e
        if e.type == "chapter":
            return e, None
        chs = [c for c in e.children if c.type == "chapter"]
        if chs:
            return chs[-1], None
        return "need-chapter", None

    def new_item(self, etype):
        if not self.project:
            return
        if etype == "chapter":
            return self.new_chapter()
        label = TYPE_LABELS[etype]
        default = self.project.unique_name(f"New {label}")
        title, ok = QInputDialog.getText(self, f"New {label.lower()}", "Name:", text=default)
        if ok and title.strip():
            self._create(etype, title)

    def new_chapter(self):
        p = self.project
        parent, after = self._outline_target("chapter")
        pos = p.new_chapter_position(parent, after)
        d = NewChapterDialog(p, p.default_chapter_name(pos), self)
        d.renumber.toggled.connect(lambda *_: d._check())
        if not d.exec():
            return
        self.editor.flush()
        try:
            _, scene_path = p.create_chapter(d.title.text(), d.scene.text(), parent, after,
                                             renumber=d.renumber.isVisible() and d.renumber.isChecked())
        except (ModelError, OSError) as ex:
            QMessageBox.warning(self, "Couldn't create chapter", str(ex))
            p.load()
            scene_path = None
        self.editor.path = None
        self.rebuild_tree()
        self._rewatch()
        self._update_stats()
        if scene_path:
            self.select(scene_path)
            self.editor.load(scene_path)
            self.editor.fields["summary"].setFocus()

    def _create(self, etype, title):
        self.editor.flush()
        parent = after = None
        if etype in OUTLINE_TYPES:
            parent, after = self._outline_target(etype)
            if parent == "need-chapter":
                QMessageBox.information(self, "Add a chapter first",
                                        "Scenes live inside chapters. Create a chapter first.")
                return
        try:
            path = self.project.create(etype, title, parent, after)
        except (ModelError, OSError) as ex:
            QMessageBox.warning(self, "Couldn't create", str(ex))
            return
        self.rebuild_tree()
        self._rewatch()
        self.select(path)
        self._update_stats()
        self.editor.title.setFocus()
        self.editor.title.selectAll()

    def _do_rename(self, old_path, new_title):
        if not self.project:
            return
        e = self.project.get(old_path)
        if e is None or new_title == e.name:
            return
        self.editor.flush()
        editing_it = self.editor.path == e.path
        cur = self.current_entity()
        cur_path = cur.path if cur else None
        try:
            new_path = self.project.rename(e, new_title)
        except (ModelError, OSError) as ex:
            QMessageBox.warning(self, "Couldn't rename", str(ex))
            if editing_it:
                self.editor.revert_title()
            return
        if cur_path == old_path or editing_it:
            cur_path = new_path
        elif cur_path and old_path.parent in cur_path.parents and e.is_container:
            cur_path = new_path.parent / cur_path.relative_to(old_path.parent)
        self.editor.path = None
        self.rebuild_tree()
        self._rewatch()
        if cur_path and self.project.get(cur_path):
            self.select(cur_path)
            self.editor.load(cur_path)
        self._update_backlinks()
        self.statusBar().showMessage(f"Renamed to “{Path(new_path).stem}” and updated links.", 5000)

    def rename_current(self):
        e = self.current_entity()
        if not e:
            return
        if e.type == "project":
            self.editor.title.setFocus()
            self.editor.title.selectAll()
            return
        title, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        if ok and title.strip():
            self._do_rename(e.path, title)

    def delete_current(self):
        e = self.current_entity()
        if not e or e.type == "project":
            return
        extra = ""
        if e.is_container:
            n = sum(1 for _ in self._iter_scenes(e))
            extra = f"\n\nThis also removes everything inside it ({n} scene{'s' if n != 1 else ''})."
        if e.type == "scene" and e.parent is not None and \
                [c for c in e.parent.children if c.type == "scene"] == [e]:
            box = QMessageBox(QMessageBox.Question, "Delete",
                              f"“{e.name}” is the only scene in {e.parent.name}, and a chapter needs "
                              "at least one scene.", parent=self)
            b_ch = box.addButton(f"Delete {e.parent.name}", QMessageBox.DestructiveRole)
            b_sc = box.addButton("Delete just the scene", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is b_ch:
                e = e.parent
            elif box.clickedButton() is not b_sc:
                return
        else:
            r = QMessageBox.question(self, "Delete", f"Move “{e.name}” to the project's .trash folder?{extra}")
            if r != QMessageBox.Yes:
                return
        self.editor.flush()
        self.editor.clear()
        try:
            self.project.delete(e)
        except OSError as ex:
            QMessageBox.warning(self, "Couldn't delete", str(ex))
        self.rebuild_tree()
        self._rewatch()
        self._update_backlinks()
        self._update_stats()

    def _do_move(self, path, parent_path, index):
        if not self.project:
            return
        e = self.project.get(Path(path))
        parent = self.project.get(Path(parent_path)) if parent_path else None
        if e is None:
            return
        self.editor.flush()
        try:
            new_path = self.project.move(e, parent, index)
        except (ModelError, OSError) as ex:
            self.statusBar().showMessage(str(ex), 6000)
            return
        self.editor.path = None
        self.rebuild_tree()
        self._rewatch()
        self.select(new_path)
        self.editor.load(new_path)

    def move_step(self, delta):
        e = self.current_entity()
        if not e or e.type not in OUTLINE_TYPES:
            return
        sibs = self.project.siblings(e)
        i = sibs.index(e)
        j = i + delta
        if 0 <= j < len(sibs):
            self._do_move(str(e.path), str(e.parent.path) if e.parent else None, j)

    def renumber_chapters(self):
        if not self.project:
            return
        plan = self.project.renumber_plan()
        if not plan:
            QMessageBox.information(self, "Renumber chapters",
                                    "Every chapter named “Chapter N” already matches its position.\n\n"
                                    "(Only chapters whose names start with “Chapter” or “Ch” and a number "
                                    "are renamed.)")
            return
        lines = "\n".join(f"{ch.name}  →  {new}" for ch, new in plan[:20])
        if len(plan) > 20:
            lines += f"\n…and {len(plan) - 20} more"
        if QMessageBox.question(self, "Renumber chapters",
                                f"Rename {len(plan)} chapter(s)? Links are updated too.\n\n{lines}") != QMessageBox.Yes:
            return
        self.editor.flush()
        cur = self.current_entity()
        cur_name = cur.name if cur and cur.type != "chapter" else None
        try:
            n = self.project.renumber_chapters()
        except (ModelError, OSError) as ex:
            QMessageBox.warning(self, "Couldn't renumber", str(ex))
            n = 0
        self.project.load()
        self.editor.path = None
        self.rebuild_tree()
        self._rewatch()
        target = self.project.resolve(cur_name) if cur_name else None
        if target:
            self.select(target.path)
            self.editor.load(target.path)
        else:
            self.editor.clear()
        self.statusBar().showMessage(f"Renumbered {n} chapter(s).", 5000)

    def _context_menu(self, pos):
        it = self.tree.itemAt(pos)
        if it:
            self.tree.setCurrentItem(it)
        e = self.current_entity()
        k = SideTree.key_of(it)
        m = QMenu(self)
        if k == "g:outline" or (e and e.type in OUTLINE_TYPES):
            m.addActions([self.a_part, self.a_chapter, self.a_scene])
        elif k.startswith("g:character") or (e and e.type == "character"):
            m.addAction(self.a_char)
        elif k.startswith("g:place") or (e and e.type == "place"):
            m.addAction(self.a_place)
        elif k.startswith("g:codex") or (e and e.type == "codex"):
            m.addAction(self.a_codex)
        elif k.startswith("g:note") or (e and e.type == "note"):
            m.addAction(self.a_note)
        if e and e.type != "project":
            m.addSeparator()
            if e.type in OUTLINE_TYPES:
                m.addActions([self.a_up, self.a_down])
            if e.type == "scene" and e.meta.get("prose"):
                m.addAction(self.a_open_draft)
            m.addActions([self.a_rename, self.a_delete])
        if not m.isEmpty():
            m.exec(self.tree.viewport().mapToGlobal(pos))

    # ================================================================== prose
    def _launch(self, path: Path):
        cmd = self.settings.value("editor_cmd", "", str).strip() or detect_focuswriter()
        if cmd:
            if Path(cmd).exists():
                args = [cmd]
            else:
                args = shlex.split(cmd, posix=not sys.platform.startswith("win"))
                args = [a.strip('"') for a in args]
            try:
                subprocess.Popen(args + [str(path)])
                return
            except OSError as ex:
                self.statusBar().showMessage(f"Couldn't start “{cmd}”: {ex}. Using the default app.", 8000)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_prose_path(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "Draft not found", f"There's no file at:\n{path}")
            return
        self._launch(path)

    def open_prose_current(self):
        e = self.current_entity()
        if not e or e.type != "scene":
            return
        self.editor.flush()
        p = self.project.prose_path(e)
        if p:
            self.open_prose_path(p)
        else:
            self.statusBar().showMessage("This scene has no linked draft yet.", 4000)

    def browse_prose(self):
        e = self.project.get(self.editor.path) if self.project and self.editor.path else None
        if not e:
            return
        start = str(self.project.prose_path(e) or self.project.root / "drafts")
        f, _ = QFileDialog.getOpenFileName(self, "Link a draft file", start, PROSE_FILTER)
        if f:
            self.editor.set_prose(self.project.prose_value(Path(f)))
            self._rewatch()
            self._update_stats()

    def create_prose(self):
        e = self.project.get(self.editor.path) if self.project and self.editor.path else None
        if not e:
            return
        drafts = self.project.root / "drafts"
        drafts.mkdir(exist_ok=True)
        f, _ = QFileDialog.getSaveFileName(self, "New draft file", str(drafts / f"{e.name}.txt"),
                                           "Plain text (*.txt);;Markdown (*.md);;All files (*)")
        if not f:
            return
        fp = Path(f)
        if not fp.exists():
            try:
                fp.write_text("", encoding="utf-8")
            except OSError as ex:
                QMessageBox.warning(self, "Couldn't create draft", str(ex))
                return
        self.editor.set_prose(self.project.prose_value(fp))
        self._rewatch()
        self._launch(fp)

    def import_drafts(self):
        if not self.project:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Import draft files as scenes",
                                                str(self.project.root), PROSE_FILTER)
        if not files:
            return
        chapters = sorted(self.project.by_type("chapter"),
                          key=lambda c: self.project.outline_pos.get(c.path, 0))
        cur = self.current_entity()
        cur_ch = self.project.chapter_of(cur) if cur and cur.type in OUTLINE_TYPES else None
        options = [c.name for c in chapters] + ["(new chapter…)"]
        idx = chapters.index(cur_ch) if cur_ch in chapters else 0
        choice, ok = QInputDialog.getItem(self, "Import drafts", f"Add {len(files)} scene(s) to chapter:",
                                          options, idx, False)
        if not ok:
            return
        self.editor.flush()
        try:
            if choice == "(new chapter…)":
                name, ok = QInputDialog.getText(self, "New chapter", "Chapter name:",
                                                text=self.project.unique_name("Imported"))
                if not ok or not name.strip():
                    return
                ch_path = self.project.create("chapter", name)
                chapter = self.project.get(ch_path)
            else:
                chapter = chapters[options.index(choice)]
            ch_path = chapter.path
            last = None
            for f in sorted(files, key=lambda s: natural_key(Path(s).name)):
                chapter = self.project.get(ch_path)
                name = self.project.unique_name(Path(f).stem)
                last = self.project.create("scene", name, chapter, after=chapter.children[-1] if chapter.children else None,
                                           meta={"status": "drafted",
                                                 "prose": self.project.prose_value(Path(f))})
        except (ModelError, OSError) as ex:
            QMessageBox.warning(self, "Import stopped", str(ex))
            last = None
        self.rebuild_tree()
        self._rewatch()
        if last:
            self.select(last)
        self._update_stats()

    # ================================================================ status
    def preferences(self):
        if Preferences(self.settings, self).exec() and self.wordlog:
            self.wordlog.day_start_hour = self._day_start()
            self._update_stats()
            self._refresh_views()

    def _day_start(self) -> int:
        try:
            return int(self.settings.value("day_start_hour", DEFAULT_DAY_START))
        except (TypeError, ValueError):
            return DEFAULT_DAY_START

    def _daily_goal(self) -> int:
        pe = self.project.project_entity if self.project else None
        try:
            return max(int(pe.meta.get("daily_goal") or 0), 0) if pe else 0
        except (TypeError, ValueError):
            return 0

    def _track_words(self) -> str:
        """Log draft changes for today's count; returns the status-bar text for today."""
        log = self.wordlog
        if not log:
            return ""
        changed = False
        if not log.error:
            counts, linked = draft_counts(self.project)
            changed = log.sync(counts, linked)
        goal = self._daily_goal()
        if goal != self.progress.goal:
            self.progress.set_goal(goal)
            changed = True
        if changed and self.tabs.currentWidget() is self.progress:
            self.progress.refresh()
        if log.error:
            return "Today: word log unreadable"
        t = log.words_on(log.today())
        if goal:
            return f"Today: {t:,} / {goal:,}" + (" ✓" if t >= goal else "")
        return f"Today: {t:,} words"

    def _update_stats(self):
        if not self.project:
            self.stats_label.clear()
            return
        today = self._track_words()
        s = self.project.stats()
        words = f"{s['words']:,}"
        if s["target"]:
            pct = 100 * s["words"] / s["target"]
            words += f" / {s['target']:,} words ({pct:.0f}%)"
        else:
            words += " words"
        parts = [f"{s['chapters']} chapters", f"{s['scenes']} scenes", words]
        if today:
            parts.insert(0, today)
        by = s["status"]
        status_bits = [f"{by[k]} {k}" for k in STATUSES if by.get(k)]
        if status_bits:
            parts.append(", ".join(status_bits))
        self.stats_label.setText("   ·   ".join(parts) + "  ")

    def _focus_tag_filter(self):
        self.filter.setFocus()
        t = self.filter.text()
        if not t.endswith("#"):
            self.filter.setText((t + " " if t and not t.endswith(" ") else t) + "#")
        self.filter.end(False)
        self.filter_completer.setCompletionPrefix("#")
        self.filter_completer.complete()

    def set_theme(self, theme):
        self.settings.setValue("theme", theme)
        apply_theme(QApplication.instance(), theme)
        self.theme_actions[theme].setChecked(True)
        self.editor.apply_theme()
        self.cork.canvas.update()
        for_all = self.tree.palette().placeholderText()

        def recolor(item):
            for i in range(item.childCount()):
                item.child(i).setForeground(1, for_all)
                recolor(item.child(i))
        recolor(self.tree.invisibleRootItem())
        if self.project:
            self._update_backlinks()
            self._refresh_views()

    def about(self):
        QMessageBox.about(self, "About Plotweaver",
                          f"<b>Plotweaver {__version__}</b><p>A novel outliner that stores everything as "
                          "plain Markdown with YAML properties, compatible with Obsidian.</p>")

    def closeEvent(self, event):
        self.editor.flush()
        super().closeEvent(event)
