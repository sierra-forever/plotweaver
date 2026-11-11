"""The project model: a folder of Markdown notes, Obsidian-compatible.

Layout
    project.md                      novel-level properties
    outline/<Part>/<Part>.md        a part (folder note)
    outline/<Part>/<Chapter>/<Chapter>.md   a chapter (folder note)
    outline/<Chapter>/<Scene>.md    scenes live inside their chapter's folder
    characters/, places/, codex/, notes/

The file name is the note's name, exactly as in Obsidian, so [[Name]] links resolve
by file name (or by an entry in `aliases`). Reading order comes from the `order`
property, so reordering never renames files or breaks links.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import mdio, prose

OUTLINE_DIR = "outline"
TYPE_DIRS = {"character": "characters", "place": "places", "codex": "codex", "note": "notes"}
DIR_TYPES = {v: k for k, v in TYPE_DIRS.items()}
EXCLUDED_DIRS = {".trash", ".obsidian", ".git", "drafts", "node_modules", "__pycache__"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
ATTACH_EXTS = IMAGE_EXTS | {".pdf", ".svg", ".mp3", ".wav", ".mp4", ".webm", ".txt", ".docx", ".odt", ".rtf"}
IMAGES_DIR = "images"
OUTLINE_TYPES = ("part", "chapter", "scene")
CONTAINER_TYPES = ("part", "chapter")
ALL_TYPES = ("project",) + OUTLINE_TYPES + tuple(TYPE_DIRS)
STATUSES = ["idea", "outlined", "drafted", "revised", "done"]
TYPE_LABELS = {"project": "Project", "part": "Part", "chapter": "Chapter", "scene": "Scene",
               "character": "Character", "place": "Place", "codex": "Codex entry", "note": "Note"}

LINK_RE = re.compile(r"\[\[([^\[\]|#^\n]+?)\s*(?:[#^][^\[\]|\n]*)?(?:\|[^\[\]\n]*)?\]\]")
TAG_RE = re.compile(r"(?<![\w#&/\\])#([\w/\-]*[^\W\d][\w/\-]*)")
CHAPTER_NUM_RE = re.compile(r"^(chapter|ch\.?)(\s*)(\d+)\b(.*)$", re.I)
# Relationship kinds offered in the editor. Directed kinds read "holder <verb> other";
# any other kind someone types is allowed and treated as mutual.
REL_KINDS = ["lover", "attraction", "harem sister", "friend", "family", "ally", "rival",
             "enemy", "mentor", "serves"]
REL_DIRECTED = {"attraction": "is drawn to", "mentor": "mentors", "serves": "serves"}
REL_KEYS = ("with", "kind", "intensity", "since", "until", "note")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


class ModelError(Exception):
    """A user-facing problem (name taken, invalid move, ...)."""


def clean_title(title: str) -> str:
    t = _BAD_CHARS.sub(" ", title or "")
    t = re.sub(r"\s+", " ", t).strip().strip(".").strip()
    return t


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def strip_link(value) -> str:
    v = str(value or "").strip()
    m = re.fullmatch(r"\[\[(.+?)(?:\|.*)?\]\]", v)
    if m:
        v = m.group(1)
        v = re.split(r"[#^]", v)[0]
    return v.split("/")[-1].strip()


def make_link(name) -> str:
    name = strip_link(name)
    return f"[[{name}]]" if name else ""


def as_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None and v != ""]
    return [value]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".pwtmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _rename(src: Path, dst: Path):
    if src == dst:
        return
    if dst.exists() and str(src).lower() != str(dst).lower():
        raise ModelError(f"“{dst.name}” already exists.")
    if str(src).lower() == str(dst).lower():  # case-only change on a case-insensitive disk
        tmp = src.with_name(src.name + ".pwtmp")
        src.rename(tmp)
        tmp.rename(dst)
    else:
        src.rename(dst)


def clean_tag(t) -> str:
    return str(t).strip().lstrip("#").strip()


class Query:
    """Filter text: words match names/aliases/summary, #tags match tags (nested tags too:
    #magic matches #magic/fire). Every term has to match."""

    def __init__(self, text: str = ""):
        toks = (text or "").split()
        self.tags = [clean_tag(t).lower() for t in toks if t.startswith("#") and len(t) > 1]
        self.words = [t.lower() for t in toks if not t.startswith("#")]

    def __bool__(self):
        return bool(self.tags or self.words)

    def tag_match(self, e) -> bool:
        et = [t.lower() for t in e.tags]
        return all(any(x == t or x.startswith(t + "/") for x in et) for t in self.tags)

    def matches(self, e) -> bool:
        if not self.tag_match(e):
            return False
        if self.words:
            hay = " ".join([e.name, *e.aliases, str(e.meta.get("summary") or "")]).lower()
            return all(w in hay for w in self.words)
        return True


@dataclass(eq=False)
class Relationship:
    """One entry in a character's `relationships:` list.

    It belongs to the pair, not just to the note it's written on: the graph shows it
    the same whichever of the two characters holds it."""
    holder: "Entity"       # the character note the entry is written in
    index: int             # position in that note's list
    other: "Entity"        # who it's with
    kind: str
    intensity: int | None  # 0-5, None when not set
    since: object          # raw value: a [[chapter or scene]] link, a chapter number, or ""
    until: object
    note: str

    @property
    def directed(self) -> bool:
        return self.kind.lower() in REL_DIRECTED

    def pair(self) -> frozenset:
        return frozenset((self.holder.path, self.other.path))

    def describe(self) -> str:
        verb = REL_DIRECTED.get(self.kind.lower())
        if verb:
            return f"{self.holder.name} {verb} {self.other.name}"
        return f"{self.holder.name} & {self.other.name}: {self.kind or 'related'}"


@dataclass(eq=False)
class Entity:
    path: Path
    meta: dict
    body: str
    mtime: float
    type: str
    broken: bool = False
    parse_error: str = ""   # why the properties couldn't be read (when broken)
    repaired: list = field(default_factory=list)  # lines quoted to make the properties readable
    parent: "Entity | None" = None
    children: list = field(default_factory=list)
    links: dict = field(default_factory=dict)  # lowercase target -> name as written
    tags: list = field(default_factory=list)   # tags from the property and inline #tags

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def title(self) -> str:
        if self.type == "project":
            return str(self.meta.get("title") or self.path.parent.name)
        return self.name

    @property
    def aliases(self) -> list[str]:
        return [str(a).strip() for a in as_list(self.meta.get("aliases")) if str(a).strip()]

    @property
    def order(self) -> float:
        try:
            return float(self.meta.get("order"))
        except (TypeError, ValueError):
            return 1e9

    @property
    def status(self) -> str:
        s = self.meta.get("status")
        return str(s) if s else ""

    @property
    def is_container(self) -> bool:
        return self.type in CONTAINER_TYPES

    def sort_key(self):
        return (self.order, natural_key(self.name))


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.entities: dict[Path, Entity] = {}
        self.names: dict[str, Entity] = {}
        self.outline_pos: dict[Path, int] = {}
        self.attachments: dict[str, Path] = {}
        self._prose_cache: dict[Path, tuple] = {}
        self.load()

    # ------------------------------------------------------------------ setup
    @staticmethod
    def is_project(root) -> bool:
        return (Path(root) / "project.md").is_file()

    @classmethod
    def new_project(cls, root, title: str) -> "Project":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        for d in (OUTLINE_DIR, *TYPE_DIRS.values()):
            (root / d).mkdir(exist_ok=True)
        pf = root / "project.md"
        if not pf.exists():
            _write(pf, mdio.dump({"type": "project", "title": title or root.name}, ""))
        return cls(root)

    # ---------------------------------------------------------------- loading
    def _iter_md(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
            for f in filenames:
                if f.lower().endswith(".md"):
                    yield Path(dirpath) / f

    def watch_dirs(self) -> list[str]:
        out = [str(self.root)]
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
            out.extend(str(Path(dirpath) / d) for d in dirnames)
        return out

    def _infer_type(self, path: Path, meta: dict) -> str:
        rel = path.relative_to(self.root).parts
        declared = meta.get("type") if isinstance(meta.get("type"), str) else None
        if len(rel) == 1 and path.name.lower() == "project.md":
            return "project"
        if rel[0] == OUTLINE_DIR:
            if len(rel) >= 3 and path.stem == path.parent.name:  # folder note
                return "part" if declared == "part" else "chapter"
            if len(rel) >= 3:
                return "scene"
            return "note"
        if declared in ALL_TYPES and declared != "project":
            return declared
        if rel[0] in DIR_TYPES and len(rel) >= 2:
            return DIR_TYPES[rel[0]]
        return "note"

    def _read(self, path: Path) -> Entity:
        text = path.read_text(encoding="utf-8", errors="replace")
        r = mdio.parse_ex(text)
        return Entity(path, r.meta, r.body, path.stat().st_mtime, self._infer_type(path, r.meta),
                      broken=not r.ok, parse_error=r.error, repaired=r.repaired)

    def _index_attachments(self):
        self.attachments = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS - {"drafts"} and not d.startswith(".")]
            for f in filenames:
                if Path(f).suffix.lower() in IMAGE_EXTS:
                    self.attachments.setdefault(f.lower(), Path(dirpath) / f)

    def load(self):
        self._index_attachments()
        self.entities = {}
        for p in self._iter_md():
            try:
                self.entities[p] = self._read(p)
            except OSError:
                pass
        self._reindex()

    def rescan(self) -> set[Path]:
        """Pick up changes made outside the app. Returns the paths that changed."""
        found = set(self._iter_md())
        before = dict(self.attachments)
        self._index_attachments()
        changed = set() if before == self.attachments else {self.root / IMAGES_DIR}
        for p in list(self.entities):
            if p not in found:
                del self.entities[p]
                changed.add(p)
        for p in found:
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            e = self.entities.get(p)
            if e is None or e.mtime != m:
                try:
                    self.entities[p] = self._read(p)
                    changed.add(p)
                except OSError:
                    pass
        if changed:
            self._reindex()
        return changed

    # ---------------------------------------------------------------- images
    def image_path(self, value) -> Path | None:
        """Resolve an image reference: an Obsidian [[file.png]] link, or a path."""
        if not value:
            return None
        v = str(value).strip()
        if v.startswith("!"):
            v = v[1:]
        if v.startswith("[["):
            name = strip_link(v)
            return self.attachments.get(name.lower())
        p = Path(v).expanduser()
        if not p.is_absolute():
            p = self.root / p
        if p.exists():
            return p
        return self.attachments.get(p.name.lower())

    def import_image(self, src, base_name: str) -> str:
        """Copy an image into images/ (unless it's already in the project) and return a
        [[link]] to it. Files are named after the character: "Sera Vale.png", "Sera Vale 2.png"."""
        src = Path(src).resolve()
        try:
            src.relative_to(self.root)
            inside = True
        except ValueError:
            inside = False
        if inside and self.attachments.get(src.name.lower()) == src:
            return make_link(src.name)
        # the same picture dropped twice: reuse the copy already in the project
        size = src.stat().st_size
        digest = None
        for name, existing in self.attachments.items():
            try:
                if existing.stat().st_size != size:
                    continue
                if digest is None:
                    digest = hashlib.sha1(src.read_bytes()).hexdigest()
                if hashlib.sha1(existing.read_bytes()).hexdigest() == digest:
                    return make_link(existing.name)
            except OSError:
                continue
        dest_dir = self.root / IMAGES_DIR
        dest_dir.mkdir(exist_ok=True)
        base = clean_title(base_name) or "image"
        ext = src.suffix.lower()
        n = 1
        while True:
            name = f"{base}{ext}" if n == 1 else f"{base} {n}{ext}"
            if name.lower() not in self.attachments and not (dest_dir / name).exists():
                break
            n += 1
        dest = dest_dir / name
        shutil.copy2(src, dest)
        self.attachments[name.lower()] = dest
        return make_link(name)

    def _extract_links(self, e: Entity) -> dict:
        out = {}
        for s in [e.body, *_strings(e.meta)]:
            for m in LINK_RE.finditer(s):
                name = m.group(1).split("/")[-1].strip()
                if name and Path(name).suffix.lower() not in ATTACH_EXTS:
                    out.setdefault(name.lower(), name)
        return out

    @staticmethod
    def _extract_tags(e: Entity) -> list:
        raw = e.meta.get("tags")
        vals = []
        if isinstance(raw, str):
            vals = re.split(r"[,\s]+", raw)
        else:
            vals = [str(v) for v in as_list(raw)]
        body = re.sub(r"```.*?```", "", e.body, flags=re.S)
        vals += TAG_RE.findall(body)
        out, seen = [], set()
        for v in vals:
            v = clean_tag(v)
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        return out

    def _parent_note(self, e: Entity) -> Path | None:
        outline = self.root / OUTLINE_DIR
        folder = e.path.parent
        if e.is_container:
            folder = folder.parent
        if folder == outline or outline not in folder.parents:
            return None
        cand = folder / f"{folder.name}.md"
        return cand if cand != e.path else None

    def _reindex(self):
        ents = sorted(self.entities.values(), key=lambda x: natural_key(str(x.path)))
        self.names = {}
        for e in ents:
            e.parent, e.children = None, []
            e.links = self._extract_links(e)
            e.tags = self._extract_tags(e)
            if e.type != "project":
                self.names.setdefault(e.name.lower(), e)
        for e in ents:
            if e.type != "project":
                for a in e.aliases:
                    self.names.setdefault(a.lower(), e)
        for e in ents:
            if e.type in OUTLINE_TYPES:
                pn = self._parent_note(e)
                if pn and pn in self.entities:
                    e.parent = self.entities[pn]
                    e.parent.children.append(e)
        for e in ents:
            e.children.sort(key=Entity.sort_key)
        self.outline_pos = {}

        def walk(items):
            for it in items:
                self.outline_pos[it.path] = len(self.outline_pos)
                walk(it.children)
        walk(self.outline_roots())

    # ---------------------------------------------------------------- queries
    @property
    def project_entity(self) -> Entity | None:
        return self.entities.get(self.root / "project.md")

    def get(self, path) -> Entity | None:
        return self.entities.get(Path(path)) if path else None

    def outline_roots(self) -> list[Entity]:
        return sorted((e for e in self.entities.values()
                       if e.type in OUTLINE_TYPES and e.parent is None), key=Entity.sort_key)

    def siblings(self, e: Entity) -> list[Entity]:
        return list(e.parent.children) if e.parent else self.outline_roots()

    def by_type(self, t: str) -> list[Entity]:
        return sorted((e for e in self.entities.values() if e.type == t),
                      key=lambda x: natural_key(x.name))

    def resolve(self, name) -> Entity | None:
        return self.names.get(strip_link(name).lower())

    def names_for(self, types=None) -> list[str]:
        return sorted((e.name for e in self.entities.values()
                       if e.type != "project" and (types is None or e.type in types)),
                      key=natural_key)

    def category_of(self, e: Entity) -> str:
        c = e.meta.get("category")
        if c:
            return str(c).strip()
        rel = e.path.relative_to(self.root).parts
        if len(rel) >= 3 and rel[0] == TYPE_DIRS["codex"]:
            return rel[1]
        return "General"

    def values_of(self, key: str, types=None) -> list[str]:
        vals = set()
        for e in self.entities.values():
            if types is None or e.type in types:
                for v in as_list(e.meta.get(key)):
                    if isinstance(v, (str, int, float)):
                        vals.add(str(v))
        return sorted(vals, key=natural_key)

    def all_tags(self) -> list[str]:
        c = Counter()
        first = {}
        for e in self.entities.values():
            for t in e.tags:
                c[t.lower()] += 1
                first.setdefault(t.lower(), t)
        return sorted((first[k] for k in c), key=natural_key)

    def scenes_in_order(self, under: Entity | None = None) -> list[Entity]:
        items = under.children if under else self.outline_roots()
        out = []

        def walk(xs):
            for x in xs:
                if x.type == "scene":
                    out.append(x)
                walk(x.children)
        walk(items)
        return out

    def chapters_in_order(self, under: Entity | None = None) -> list[Entity]:
        items = under.children if under else self.outline_roots()
        out = []

        def walk(xs):
            for x in xs:
                if x.type == "chapter":
                    out.append(x)
                elif x.type == "part":
                    walk(x.children)
        walk(items)
        return out

    def chapter_number(self, ch: Entity) -> int | None:
        for i, c in enumerate(self.chapters_in_order(), 1):
            if c is ch:
                return i
        return None

    def new_chapter_position(self, parent: Entity | None, after: Entity | None) -> int:
        """1-based position a chapter created at (parent, after) will have."""
        chs = self.chapters_in_order()
        if after is not None and after in chs:
            return chs.index(after) + 2
        if parent is not None:
            mine = [c for c in parent.children if c.type == "chapter"]
            if mine:
                return chs.index(mine[-1]) + 2
            pp = self.outline_pos.get(parent.path, 0)
            return sum(1 for c in chs if self.outline_pos.get(c.path, 0) < pp) + 1
        return len(chs) + 1

    def default_chapter_name(self, position: int) -> str:
        chs = self.chapters_in_order()
        if not chs or any(CHAPTER_NUM_RE.match(c.name) for c in chs):
            return f"Chapter {position}"
        return self.unique_name("New Chapter")

    def create_chapter(self, title: str, scene_title: str, parent: Entity | None = None,
                       after: Entity | None = None, renumber: bool = False) -> tuple[Path, Path]:
        """Create a chapter together with its first scene (a chapter always has one).
        With renumber=True and a taken "Chapter N" title, later chapters shift up to make room."""
        title = clean_title(title)
        scene_title = clean_title(scene_title) or self.unique_name("Untitled scene")
        if not title:
            raise ModelError("The chapter needs a name.")
        if title.lower() == scene_title.lower():
            raise ModelError("The scene needs a different name from its chapter "
                             "(they'd be the same file).")
        if self.name_taken(scene_title):
            raise ModelError(f"Something is already named “{scene_title}”.")
        if renumber and self.name_taken(title) and CHAPTER_NUM_RE.match(title):
            tmp = self.unique_name("pw-new-chapter")
            self.create("chapter", tmp, parent, after)
            self.renumber_chapters()
            ch_path = self.rename(self.resolve(tmp), title)
        else:
            ch_path = self.create("chapter", title, parent, after)
        sc_path = self.create("scene", scene_title, self.get(ch_path))
        return ch_path, sc_path

    def renumber_plan(self) -> list[tuple[Entity, str]]:
        """Chapters named like "Chapter 7 - Title" whose number doesn't match their position."""
        plan = []
        for i, ch in enumerate(self.chapters_in_order(), 1):
            m = CHAPTER_NUM_RE.match(ch.name)
            if m and int(m.group(3)) != i:
                plan.append((ch, f"{m.group(1)}{m.group(2) or ' '}{i}{m.group(4)}"))
        return plan

    def renumber_chapters(self) -> int:
        plan = self.renumber_plan()
        if not plan:
            return 0
        movers = {ch.path for ch, _ in plan}
        for ch, new in plan:
            hit = self.names.get(new.lower())
            if hit is not None and hit.path not in movers:
                raise ModelError(f"Can't renumber: “{new}” is already used by another note.")
        # two passes so "Chapter 5" -> "Chapter 1" never collides with the old "Chapter 1"
        temps = []
        for i, (ch, new) in enumerate(plan):
            tmp = self.unique_name(f"pw-renumber-{i}")
            self.rename(self.resolve(ch.name), tmp)
            temps.append((tmp, new))
        for tmp, new in temps:
            self.rename(self.resolve(tmp), new)
        return len(plan)

    @staticmethod
    def snippet(e: Entity, limit: int = 400) -> str:
        """First lines of a note's text, with link and list markup removed."""
        lines = []
        for line in e.body.splitlines():
            t = line.strip()
            if not t or t.startswith("```"):
                continue
            t = re.sub(r"^#+\s*", "", t)
            t = re.sub(r"^([-*+]|\d+\.)\s+(\[.\]\s*)?", "• ", t)
            t = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", t)
            t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
            lines.append(t)
            if sum(len(x) for x in lines) > limit:
                break
        return "\n".join(lines)[:limit]

    def backlinks(self, e: Entity) -> list[Entity]:
        keys = {e.name.lower(), *(a.lower() for a in e.aliases)}
        hits = [x for x in self.entities.values() if x is not e and keys & x.links.keys()]
        order = {t: i for i, t in enumerate(ALL_TYPES)}
        return sorted(hits, key=lambda x: (order.get(x.type, 99),
                                           self.outline_pos.get(x.path, 0), natural_key(x.name)))

    def unresolved(self, e: Entity) -> list[str]:
        return sorted((name for low, name in e.links.items() if low not in self.names),
                      key=natural_key)

    def chapter_of(self, e: Entity) -> Entity | None:
        while e is not None and e.type != "chapter":
            e = e.parent
        return e

    # ---------------------------------------------------------------- relationships
    def scene_cast(self, scene: Entity) -> dict:
        """Character path -> 2 for the POV, 1 for listed in Characters."""
        out = {}
        for n in as_list(scene.meta.get("characters")):
            c = self.resolve(n)
            if c is not None and c.type == "character":
                out[c.path] = 1
        if scene.meta.get("pov"):
            c = self.resolve(scene.meta.get("pov"))
            if c is not None and c.type == "character":
                out[c.path] = 2
        return out

    @staticmethod
    def rel_entries(e: Entity) -> list:
        """The raw `relationships:` list of a note (only the dict entries)."""
        return [r for r in as_list(e.meta.get("relationships")) if isinstance(r, dict)]

    def relationships(self) -> tuple[list[Relationship], list[str]]:
        """Every relationship in the project, plus the names they point at that
        don't resolve to a character."""
        out, missing = [], []
        for c in self.by_type("character"):
            for i, r in enumerate(as_list(c.meta.get("relationships"))):
                if not isinstance(r, dict) or not r.get("with"):
                    continue
                other = self.resolve(r.get("with"))
                if other is None or other.type != "character":
                    missing.append(strip_link(r.get("with")))
                    continue
                if other is c:
                    continue
                try:
                    inten = int(r.get("intensity"))
                    inten = max(0, min(5, inten))
                except (TypeError, ValueError):
                    inten = None
                out.append(Relationship(c, i, other, str(r.get("kind") or "").strip(), inten,
                                        r.get("since") or "", r.get("until") or "",
                                        str(r.get("note") or "").strip()))
        return out, sorted(set(missing), key=natural_key)

    def story_index(self, value) -> int | None:
        """0-based chapter index a `since`/`until` value points at.

        Accepts a [[link]] to a chapter, a scene (its chapter) or a part (its first
        chapter), or a plain chapter number. None when empty or unresolvable."""
        if value is None or value == "" or isinstance(value, bool):
            return None
        chapters = self.chapters_in_order()
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            n = int(value)
            return max(0, min(n - 1, len(chapters) - 1)) if chapters else None
        e = self.resolve(value)
        if e is None:
            return None
        if e.type == "part":
            sub = self.chapters_in_order(e)
            e = sub[0] if sub else None
        else:
            e = self.chapter_of(e)
        for i, ch in enumerate(chapters):
            if ch is e:
                return i
        return None

    def rel_state(self, r: Relationship, at: int | None) -> str:
        """"active", "ended" or "future" as of chapter index `at` (None = whole story)."""
        since, until = self.story_index(r.since), self.story_index(r.until)
        if at is None:
            return "ended" if until is not None else "active"
        if since is not None and since > at:
            return "future"
        if until is not None and until <= at:
            return "ended"
        return "active"

    def shared_scenes(self, upto: int | None = None, query: "Query | None" = None) -> dict:
        """frozenset({path, path}) -> number of scenes both characters are in,
        counting scenes in chapters up to index `upto` (None = all)."""
        chapter_idx = {ch.path: i for i, ch in enumerate(self.chapters_in_order())}
        counts = Counter()
        for s in self.scenes_in_order():
            if query is not None and query.tags and not query.tag_match(s):
                continue
            if upto is not None:
                ch = self.chapter_of(s)
                if ch is not None and chapter_idx.get(ch.path, 0) > upto:
                    continue
            cast = sorted(self.scene_cast(s), key=str)
            for i, a in enumerate(cast):
                for b in cast[i + 1:]:
                    counts[frozenset((a, b))] += 1
        return dict(counts)

    def name_taken(self, name: str, exclude: Entity | None = None) -> bool:
        hit = self.names.get(name.lower())
        return hit is not None and hit is not exclude

    def unique_name(self, base: str) -> str:
        base = clean_title(base) or "Untitled"
        if not self.name_taken(base):
            return base
        i = 2
        while self.name_taken(f"{base} {i}"):
            i += 1
        return f"{base} {i}"

    # ------------------------------------------------------------------ prose
    def prose_path(self, e: Entity) -> Path | None:
        v = e.meta.get("prose")
        if not v:
            return None
        p = Path(str(v)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def prose_value(self, path: Path) -> str:
        """Store paths inside the project relative to it, others absolute."""
        path = Path(path).resolve()
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def prose_words(self, e: Entity) -> int | None:
        p = self.prose_path(e)
        if p is None:
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        key = (st.st_mtime, st.st_size)
        cached = self._prose_cache.get(p)
        if cached and cached[0] == key:
            return cached[1]
        n = prose.word_count(p)
        self._prose_cache[p] = (key, n)
        return n

    def prose_files(self) -> list[str]:
        out = []
        for e in self.entities.values():
            p = self.prose_path(e)
            if p is not None and p.exists():
                out.append(str(p))
        return out

    def words_under(self, e: Entity) -> int:
        if e.type == "scene":
            return self.prose_words(e) or 0
        return sum(self.words_under(c) for c in e.children)

    def stats(self) -> dict:
        scenes = self.by_type("scene")
        target = 0
        pe = self.project_entity
        if pe:
            try:
                target = int(pe.meta.get("target_words") or 0)
            except (TypeError, ValueError):
                target = 0
        return {
            "scenes": len(scenes),
            "chapters": len(self.by_type("chapter")),
            "status": Counter(s.status or "idea" for s in scenes),
            "words": sum(self.prose_words(s) or 0 for s in scenes),
            "target": target,
        }

    # ---------------------------------------------------------------- editing
    def save(self, e: Entity, meta: dict | None = None, body: str | None = None):
        if e.broken:
            raise ModelError("This note's properties couldn't be read, so it is read-only here"
                             + (f" ({e.parse_error})." if e.parse_error else "."))
        if meta is not None:
            e.meta = meta
        if body is not None:
            e.body = body
        _write(e.path, mdio.dump(e.meta, e.body))
        e.mtime = e.path.stat().st_mtime
        e.repaired = []  # written back as valid YAML
        self._reindex()

    def _renumber(self, items: list[Entity]):
        for i, s in enumerate(items):
            want = (i + 1) * 10
            if s.meta.get("order") != want:
                s.meta["order"] = want
                if not s.broken:
                    _write(s.path, mdio.dump(s.meta, s.body))
                    s.mtime = s.path.stat().st_mtime

    def _check_parent(self, etype: str, parent: Entity | None):
        if etype == "scene" and (parent is None or parent.type != "chapter"):
            raise ModelError("Scenes go inside a chapter.")
        if etype == "chapter" and parent is not None and parent.type != "part":
            raise ModelError("Chapters go inside a part, or at the top of the outline.")
        if etype == "part" and parent is not None:
            raise ModelError("Parts go at the top of the outline.")

    def create(self, etype: str, title: str, parent: Entity | None = None,
               after: Entity | None = None, meta: dict | None = None) -> Path:
        title = clean_title(title) or "Untitled"
        if self.name_taken(title):
            raise ModelError(f"Something is already named “{title}”. Names must be unique "
                             "so [[links]] know where to point.")
        new_meta = {"type": etype}
        if etype in OUTLINE_TYPES:
            self._check_parent(etype, parent)
            new_meta["order"] = 0
            new_meta["status"] = "idea"
            if etype == "scene":
                path = parent.path.parent / f"{title}.md"
            else:
                base = parent.path.parent if parent else self.root / OUTLINE_DIR
                folder = base / title
                if folder.exists():
                    raise ModelError(f"A folder named “{title}” already exists there.")
                path = folder / f"{title}.md"
        else:
            path = self.root / TYPE_DIRS.get(etype, "notes") / f"{title}.md"
        if path.exists():
            raise ModelError(f"“{path.name}” already exists.")
        if meta:
            new_meta.update(meta)
        _write(path, mdio.dump(new_meta, ""))
        self.entities[path] = self._read(path)
        self._reindex()
        e = self.entities[path]
        if etype in OUTLINE_TYPES:
            sibs = [s for s in self.siblings(e) if s is not e]
            idx = sibs.index(after) + 1 if after in sibs else len(sibs)
            sibs.insert(idx, e)
            self._renumber(sibs)
            self._reindex()
        return path

    def _replace_links_everywhere(self, old_names: list[str], new: str):
        pats = [re.compile(r"\[\[\s*(?:[^\[\]|#^\n]*/)?" + re.escape(o) + r"\s*(?=[\]|#^])", re.I)
                for o in old_names]
        for p in self._iter_md():
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new_text = text
            for pat in pats:
                new_text = pat.sub("[[" + new, new_text)
            if new_text != text:
                _write(p, new_text)

    def rename(self, e: Entity, new_title: str) -> Path:
        new = clean_title(new_title)
        if not new:
            raise ModelError("A name can't be empty.")
        if e.type == "project":
            raise ModelError("Edit the project title in its properties.")
        if new == e.name:
            return e.path
        if self.name_taken(new, exclude=e):
            raise ModelError(f"Something is already named “{new}”.")
        old = e.name
        if e.is_container:
            folder = e.path.parent
            _rename(e.path, folder / f"{new}.md")
            new_folder = folder.parent / new
            _rename(folder, new_folder)
            new_path = new_folder / f"{new}.md"
        else:
            new_path = e.path.with_name(f"{new}.md")
            _rename(e.path, new_path)
        self._replace_links_everywhere([old], new)
        self.load()
        return new_path

    def move(self, e: Entity, new_parent: Entity | None, index: int) -> Path:
        """Move an outline item under new_parent (None = top level) at position index
        among its new siblings (counted without the item itself)."""
        self._check_parent(e.type, new_parent)
        if new_parent is not None and (new_parent is e or e.path.parent in new_parent.path.parents
                                       and e.is_container):
            raise ModelError("Can't move something inside itself.")
        path = e.path
        if (new_parent is not e.parent) or (new_parent is None and e.parent is not None):
            if e.is_container:
                src = e.path.parent
                base = new_parent.path.parent if new_parent else self.root / OUTLINE_DIR
                dst = base / src.name
                if dst.exists():
                    raise ModelError(f"A folder named “{src.name}” already exists there.")
                shutil.move(str(src), str(dst))
                path = dst / e.path.name
            else:
                dst = new_parent.path.parent / e.path.name
                if dst.exists():
                    raise ModelError(f"“{dst.name}” already exists there.")
                shutil.move(str(e.path), str(dst))
                path = dst
            self.load()
        e = self.entities[path]
        parent = self.entities.get(new_parent.path) if new_parent else None
        sibs = [s for s in (parent.children if parent else self.outline_roots()) if s is not e]
        index = max(0, min(index, len(sibs)))
        sibs.insert(index, e)
        self._renumber(sibs)
        self._reindex()
        return path

    def delete(self, e: Entity) -> Path:
        """Move to .trash (Obsidian's convention) rather than deleting outright."""
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        src = e.path.parent if e.is_container else e.path
        dst = trash / src.name
        if dst.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = trash / f"{src.stem} {stamp}{src.suffix}"
        shutil.move(str(src), str(dst))
        self.load()
        return dst
