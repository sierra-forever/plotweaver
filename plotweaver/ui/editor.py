"""The editor pane: title, typed properties, extra properties, and notes."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
                               QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from .. import mdio
from ..model import STATUSES, TYPE_LABELS, Entity, ModelError, Project, strip_link
from .relationships import RelationshipsEdit
from .widgets import LinkEdit, ListEdit, PortraitPanel, ProseField, WikiTextEdit

ROLES = ["protagonist", "love interest", "antagonist", "rival", "mentor", "ally", "supporting", "minor"]
CODEX_CATEGORIES = ["faction", "magic", "power", "item", "creature", "culture", "history", "lore", "glossary"]

# (key, label, kind, options)
SCHEMAS = {
    "project": [("author", "Author", "text", None), ("series", "Series", "text", None),
                ("genre", "Genre", "text", None), ("target_words", "Target words", "int", (0, 5_000_000, 1000)),
                ("deadline", "Deadline", "text", None), ("tags", "Tags", "list", "tags")],
    "part": [("status", "Status", "status", None), ("summary", "Summary", "text", None),
             ("tags", "Tags", "list", "tags")],
    "chapter": [("status", "Status", "status", None), ("summary", "Summary", "text", None),
                ("pov", "POV", "link", ("character",)), ("tags", "Tags", "list", "tags")],
    "scene": [("status", "Status", "status", None), ("summary", "Summary", "text", None),
              ("pov", "POV", "link", ("character",)), ("location", "Location", "link", ("place",)),
              ("characters", "Characters", "links", ("character",)),
              ("story_date", "Story date", "text", None), ("threads", "Threads", "list", "threads"),
              ("heat", "Heat (0–5)", "int", (0, 5, 1)), ("prose", "Draft file", "prose", None),
              ("tags", "Tags", "list", "tags")],
    "character": [("aliases", "Aliases", "list", None), ("role", "Role", "choice", ROLES),
                  ("age", "Age", "text", None), ("species", "Species / race", "text", None),
                  ("traits", "Key traits", "list", "traits"),
                  ("appearance", "Appearance", "longtext", None),
                  ("affiliation", "Affiliation", "links", ("codex",)),
                  ("relationships", "Relationships", "relationships", None), ("tags", "Tags", "list", "tags"),
                  ("portrait", "Portrait", "portrait", None), ("images", "Gallery", "gallery", None)],
    "place": [("aliases", "Aliases", "list", None), ("parent", "Located in", "link", ("place",)),
              ("tags", "Tags", "list", "tags")],
    "codex": [("category", "Category", "choice", CODEX_CATEGORIES), ("aliases", "Aliases", "list", None),
              ("tags", "Tags", "list", "tags")],
    "note": [("tags", "Tags", "list", "tags")],
}
RESERVED = {"type", "order", "title"}


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == 0


class EntityEditor(QWidget):
    saved = Signal(object)                 # path
    renameRequested = Signal(object, str)  # old path, new title
    followLink = Signal(str)
    openProse = Signal(object)             # Path
    browseProse = Signal()
    createProse = Signal()
    error = Signal(str)
    openFile = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.path = None
        self.dirty = False
        self._loading = False
        self.fields: dict[str, QWidget] = {}
        self.kinds: dict[str, str] = {}
        self.timer = QTimer(self, singleShot=True, interval=700)
        self.timer.timeout.connect(self.save)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        self.type_label = QLabel()
        self.type_label.setStyleSheet("color: palette(mid); font-weight: 600; letter-spacing: 1px;")
        self.title = QLineEdit()
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setFrame(False)
        self.title.editingFinished.connect(self._title_done)
        self.title.textEdited.connect(self._title_edited)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.apply_theme()
        self.banner.hide()
        outer.addWidget(self.type_label)
        outer.addWidget(self.title)
        outer.addWidget(self.banner)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.portrait = PortraitPanel(
            lambda v: self.project.image_path(v) if self.project else None,
            lambda path: self.project.import_image(path, self.entity().name))
        self.portrait.changed.connect(self._mark)
        self.portrait.openFile.connect(self.openFile)
        self.portrait.hide()
        form_row = QHBoxLayout()
        form_col = QVBoxLayout()
        form_col.addLayout(self.form)
        form_col.addStretch(1)
        form_row.addLayout(form_col, 1)
        form_row.addWidget(self.portrait, 0, Qt.AlignTop)
        tl.addLayout(form_row)

        # extra (custom) properties
        row = QHBoxLayout()
        self.extra_toggle = QToolButton()
        self.extra_toggle.setCheckable(True)
        self.extra_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.extra_toggle.setArrowType(Qt.RightArrow)
        self.extra_toggle.setAutoRaise(True)
        self.extra_toggle.toggled.connect(self._toggle_extras)
        row.addWidget(self.extra_toggle)
        row.addStretch(1)
        tl.addLayout(row)
        self.extras_box = QWidget()
        el = QVBoxLayout(self.extras_box)
        el.setContentsMargins(0, 0, 0, 0)
        self.extras = QTableWidget(0, 2)
        self.extras.setHorizontalHeaderLabels(["Property", "Value"])
        self.extras.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.extras.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.extras.setColumnWidth(0, 160)
        self.extras.verticalHeader().hide()
        self.extras.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.extras.setMaximumHeight(150)
        self.extras.itemChanged.connect(lambda *_: self._mark())
        bl = QHBoxLayout()
        b_add, b_del = QPushButton("Add property"), QPushButton("Remove")
        b_add.clicked.connect(self._add_extra)
        b_del.clicked.connect(self._del_extra)
        bl.addWidget(b_add)
        bl.addWidget(b_del)
        bl.addStretch(1)
        el.addWidget(self.extras)
        el.addLayout(bl)
        self.extras_box.hide()
        tl.addWidget(self.extras_box)

        self.body = WikiTextEdit(lambda n: bool(self.project and self.project.resolve(n)))
        bf = QFont(self.font())
        bf.setPointSizeF(bf.pointSizeF() * 1.1)
        self.body.setFont(bf)
        self.body.textChanged.connect(self._mark)
        self.body.linkActivated.connect(self.followLink)

        top.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        outer.addWidget(top)
        notes = QLabel("Notes")
        notes.setStyleSheet("color: palette(mid); font-weight: 600; letter-spacing: 1px;")
        outer.addSpacing(6)
        outer.addWidget(notes)
        outer.addWidget(self.body, 1)

    def apply_theme(self):
        from .theme import accent
        self.banner.setStyleSheet(f"background:{accent('banner_bg')};color:{accent('banner_fg')};"
                                  "padding:6px;border-radius:4px;")
        if hasattr(self, "body"):
            self.body.highlighter.update_colors()

    # ------------------------------------------------------------- plumbing
    def set_project(self, project: Project | None):
        self.project = project
        self.clear()

    def entity(self) -> Entity | None:
        return self.project.get(self.path) if self.project else None

    def _mark(self):
        if self._loading or self.path is None:
            return
        self.dirty = True
        self.timer.start()

    def _title_edited(self):
        e = self.entity()
        if e and e.type == "project":
            self._mark()

    def _title_done(self):
        e = self.entity()
        if not e or e.type == "project" or self._loading:
            return
        new = self.title.text().strip()
        if new and new != e.name:
            self.renameRequested.emit(e.path, new)
        elif not new:
            self.title.setText(e.name)

    def revert_title(self):
        e = self.entity()
        if e:
            self.title.setText(e.title)

    def _toggle_extras(self, on):
        self.extras_box.setVisible(on)
        self.extra_toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def _update_extra_label(self):
        self.extra_toggle.setText(f"Other properties ({self.extras.rowCount()})")

    def _add_extra(self):
        r = self.extras.rowCount()
        self.extras.insertRow(r)
        self.extras.setItem(r, 0, QTableWidgetItem("new_property"))
        self.extras.setItem(r, 1, QTableWidgetItem(""))
        self.extras.editItem(self.extras.item(r, 0))
        self._update_extra_label()
        self._mark()

    def _del_extra(self):
        rows = sorted({i.row() for i in self.extras.selectedIndexes()}, reverse=True)
        for r in rows:
            self.extras.removeRow(r)
        self._update_extra_label()
        if rows:
            self._mark()

    # --------------------------------------------------------------- loading
    def clear(self):
        self.timer.stop()
        self.path = None
        self.dirty = False
        self.setEnabled(False)
        self._loading = True
        self.title.clear()
        self.type_label.clear()
        self.body.clear()
        self._clear_form()
        self.portrait.hide()
        self.extras.setRowCount(0)
        self.banner.hide()
        self._loading = False

    def _clear_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields, self.kinds = {}, {}

    def load(self, path):
        e = self.project.get(path) if self.project else None
        if e is None:
            self.clear()
            return
        self.timer.stop()
        self._loading = True
        self.setEnabled(True)
        self.path = e.path
        self.dirty = False
        self.type_label.setText(TYPE_LABELS.get(e.type, e.type).upper())
        self.title.setText(e.title)
        self.title.setReadOnly(e.broken)
        self.title.setToolTip("Project title" if e.type == "project" else
                              "Renaming also updates every [[link]] to this note.")
        self._clear_form()
        self.portrait.setVisible(e.type == "character")
        for key, label, kind, opt in SCHEMAS.get(e.type, SCHEMAS["note"]):
            if kind in ("portrait", "gallery"):
                self.fields[key], self.kinds[key] = self.portrait, kind
                continue
            w = self._make_field(key, kind, opt, e)
            self.fields[key], self.kinds[key] = w, kind
            self.form.addRow(label, w)
        if e.type == "character":
            self.portrait.set_values(e.meta.get("portrait"), e.meta.get("images"))
        schema_keys = {k for k, *_ in SCHEMAS.get(e.type, SCHEMAS["note"])}
        self.extras.blockSignals(True)
        self.extras.setRowCount(0)
        for k, v in e.meta.items():
            if k in RESERVED or k in schema_keys:
                continue
            r = self.extras.rowCount()
            self.extras.insertRow(r)
            self.extras.setItem(r, 0, QTableWidgetItem(str(k)))
            text = mdio.dump_value(v)
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole, (text, v))
            self.extras.setItem(r, 1, item)
        self.extras.blockSignals(False)
        self._update_extra_label()
        self.body.setPlainText(e.body.lstrip("\n") if e.meta else e.body)
        self.body.setReadOnly(e.broken)
        self.refresh_options()
        if e.broken:
            where = f" ({e.parse_error})" if e.parse_error else ""
            self.banner.setText("This note's properties (the --- block at the top) aren't valid "
                                f"YAML{where}, so it's shown read-only to avoid losing anything. "
                                "Fix it in a text editor and it will reload automatically.")
            self.banner.show()
            for w in self.fields.values():
                w.setEnabled(False)
        elif e.repaired:
            lines = ", ".join(str(n) for n in e.repaired)
            plural = "s" if len(e.repaired) > 1 else ""
            self.banner.setText(f"Line{plural} {lines} of this note's properties had text YAML can't "
                                "read unquoted (usually a “: ” inside a value), so Plotweaver read it "
                                "as plain text. Check it looks right. It's written back with quotes "
                                "the next time you change this note.")
            self.banner.show()
            self.portrait.setEnabled(True)
        else:
            self.banner.hide()
            self.portrait.setEnabled(True)
        self.refresh_prose()
        self._loading = False

    def _make_field(self, key, kind, opt, e: Entity) -> QWidget:
        val = e.meta.get(key)
        if kind == "status":
            w = QComboBox()
            w.addItems(STATUSES)
            s = str(val) if val else "idea"
            if s not in STATUSES:
                w.addItem(s)
            w.setCurrentText(s)
            w.currentTextChanged.connect(self._mark)
        elif kind == "choice":
            w = QComboBox()
            w.setEditable(True)
            w.addItems([""] + list(opt))
            w.setCurrentText(str(val) if val else "")
            w.currentTextChanged.connect(self._mark)
        elif kind == "int":
            lo, hi, step = opt
            w = QSpinBox()
            w.setRange(lo, hi)
            w.setSingleStep(step)
            w.setGroupSeparatorShown(hi > 9999)
            w.setSpecialValueText("—")
            w.setMaximumWidth(160)
            try:
                w.setValue(int(val or 0))
            except (TypeError, ValueError):
                w.setValue(0)
            w.valueChanged.connect(self._mark)
        elif kind == "link":
            w = LinkEdit()
            w.set_value(val)
            w.changed.connect(self._mark)
            w.follow.connect(self.followLink)
        elif kind in ("links", "list"):
            w = ListEdit(links=(kind == "links"),
                         placeholder="e.g. silver hair, green eyes, 5'2\", scar on left cheek"
                         if key == "traits" else
                         "e.g. flashback, needs research — or type #tags in the notes"
                         if key == "tags" else "",
                         strip_hash=(key == "tags"))
            w.set_value(val)
            w.changed.connect(self._mark)
        elif kind == "relationships":
            w = RelationshipsEdit()
            w.set_value(val)
            w.changed.connect(self._mark)
            w.follow.connect(self.followLink)
        elif kind == "longtext":
            w = QPlainTextEdit()
            w.setTabChangesFocus(True)
            w.setPlainText("" if val is None else str(val))
            lh = w.fontMetrics().lineSpacing()
            w.setFixedHeight(lh * 4 + 14)
            w.textChanged.connect(self._mark)
        elif kind == "prose":
            w = ProseField()
            w.set_value(val)
            w.changed.connect(self._mark)
            w.changed.connect(lambda: QTimer.singleShot(0, self.refresh_prose))
            w.browse.connect(self.browseProse)
            w.create.connect(self.createProse)
            w.open.connect(self._open_prose)
        else:
            w = QLineEdit()
            w.setText("" if val is None else str(val))
            w.textEdited.connect(self._mark)
        return w

    def refresh_options(self):
        """Refill autocomplete lists (names change as notes are added/renamed)."""
        p = self.project
        if not p or self.path is None:
            return
        self.body.set_options(p.names_for())
        e = self.entity()
        for key, label, kind, opt in SCHEMAS.get(e.type if e else "note", []):
            w = self.fields.get(key)
            if w is None:
                continue
            if kind in ("link", "links"):
                w.set_options(p.names_for(opt))
            elif kind == "relationships":
                w.set_options([n for n in p.names_for(("character",)) if n != e.name],
                              p.names_for(("chapter", "scene", "part")))
                rels, _ = p.relationships()
                w.set_incoming([r for r in rels if r.other is e and r.holder is not e])
            elif kind == "list" and opt == "tags":
                w.set_options(p.all_tags())
            elif kind == "list" and opt:
                w.set_options(p.values_of(opt))
            elif kind == "choice":
                cur = w.currentText()
                extra = [v for v in p.values_of(key) if w.findText(v) < 0]
                if extra:
                    w.blockSignals(True)
                    w.addItems(extra)
                    w.setCurrentText(cur)
                    w.blockSignals(False)

    def refresh_prose(self):
        w = self.fields.get("prose")
        if not isinstance(w, ProseField) or not self.project:
            return
        v = w.value()
        if not v:
            w.set_words(None)
            return
        e = self.entity()
        tmp = Entity(e.path, {"prose": v}, "", 0, "scene") if e else None
        p = self.project.prose_path(tmp) if tmp else None
        exists = bool(p and p.exists())
        w.set_words(self.project.prose_words(tmp) if exists else None, exists)

    def set_prose(self, value: str):
        w = self.fields.get("prose")
        if isinstance(w, ProseField):
            w.set_value(value)
            self._mark()
            self.save()
            self.refresh_prose()

    def _open_prose(self):
        e = self.entity()
        if e:
            self.save()
            p = self.project.prose_path(self.entity())
            if p:
                self.openProse.emit(p)

    # ---------------------------------------------------------------- saving
    def collect_meta(self, e: Entity) -> dict:
        meta = {"type": e.type}
        if e.type == "project":
            meta["title"] = self.title.text().strip() or e.title
        if "order" in e.meta:
            meta["order"] = e.meta["order"]
        for key, w in self.fields.items():
            kind = self.kinds[key]
            if kind in ("status", "choice"):
                v = w.currentText().strip()
            elif kind == "int":
                v = w.value()
            elif kind in ("link", "links", "list", "prose", "relationships"):
                v = w.value()
            elif kind == "longtext":
                v = w.toPlainText().strip()
            elif kind == "portrait":
                v = w.portrait_value()
            elif kind == "gallery":
                v = w.images_value()
            else:
                v = w.text().strip()
            if not _empty(v):
                meta[key] = v
        for r in range(self.extras.rowCount()):
            k_item, v_item = self.extras.item(r, 0), self.extras.item(r, 1)
            k = (k_item.text().strip() if k_item else "")
            if not k or k in RESERVED or k in meta:
                continue
            text = v_item.text() if v_item else ""
            orig = v_item.data(Qt.UserRole) if v_item else None
            meta[k] = orig[1] if orig and orig[0] == text else mdio.load_value(text)
        return meta

    def save(self):
        self.timer.stop()
        if not self.dirty or not self.project or self.path is None:
            return
        e = self.entity()
        if e is None or e.broken:
            self.dirty = False
            return
        try:
            self.project.save(e, self.collect_meta(e), self.body.toPlainText())
        except (OSError, ModelError) as ex:
            self.error.emit(f"Couldn't save “{e.name}”: {ex}")
            return
        self.dirty = False
        self.saved.emit(e.path)

    def flush(self):
        if self.dirty:
            self.save()
