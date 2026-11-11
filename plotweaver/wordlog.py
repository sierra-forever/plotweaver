"""Daily word counts, worked out from the linked draft files.

Plotweaver doesn't see you type; it sees draft files change. So it remembers each draft's last
known word count, and when a count changes it credits the difference (net: deletions count
against the day) to the day the file was last modified. With Plotweaver open that's the moment
you saved; after it's been closed, a draft edited on several days lands on the last of them.

Rules that keep the numbers honest:
- A draft seen for the first time (newly linked, imported, or the first run) only sets a
  baseline. Linking a 30,000-word file isn't a 30,000-word day.
- An unlinked or deleted draft is forgotten, not subtracted.
- A "day" starts at ``day_start_hour`` (4 a.m. by default), so a late session counts as one day.

Stored in ``.plotweaver/wordlog.json`` inside the project, so the history travels with the novel.
Dot-folders are ignored by Plotweaver's own scan and by Obsidian."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_DIR = ".plotweaver"
LOG_NAME = "wordlog.json"
DEFAULT_DAY_START = 4


def day_of(ts: float, day_start_hour: int = DEFAULT_DAY_START) -> date:
    return (datetime.fromtimestamp(ts) - timedelta(hours=day_start_hour)).date()


class WordLog:
    def __init__(self, root: Path, day_start_hour: int = DEFAULT_DAY_START):
        self.path = Path(root) / LOG_DIR / LOG_NAME
        self.day_start_hour = day_start_hour
        self.files: dict[str, dict] = {}   # draft key -> {"words": n, "mtime": t}
        self.days: dict[str, int] = {}     # "YYYY-MM-DD" -> net words written
        self.error = ""
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as ex:
            self.error = f"Couldn't read the word log: {ex}"
            return
        files, days = data.get("files"), data.get("days")
        if isinstance(files, dict):
            self.files = {k: v for k, v in files.items()
                          if isinstance(v, dict) and isinstance(v.get("words"), int)}
        if isinstance(days, dict):
            self.days = {k: int(v) for k, v in days.items() if isinstance(v, (int, float))}

    def save(self):
        if self.error:
            return  # never overwrite a log we couldn't read
        data = {"version": 1, "files": dict(sorted(self.files.items())),
                "days": dict(sorted(self.days.items()))}
        try:
            self.path.parent.mkdir(exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as ex:
            self.error = f"Couldn't save the word log: {ex}"

    # ------------------------------------------------------------ tracking
    def today(self, now: float | None = None) -> date:
        return day_of(time.time() if now is None else now, self.day_start_hour)

    def sync(self, counts: dict[str, tuple[int, float]], linked: set[str] | None = None,
             now: float | None = None) -> bool:
        """Update from the drafts' current {key: (words, mtime)}. ``linked`` is every draft still
        linked, readable or not: one that's mid-save or briefly missing keeps its baseline.
        Returns True if anything changed."""
        keep = set(counts) | (linked or set())
        today = self.today(now)
        changed = False
        for key, (words, mtime) in counts.items():
            old = self.files.get(key)
            if old is not None and old["words"] != words:
                d = min(day_of(mtime, self.day_start_hour), today).isoformat()
                self.days[d] = self.days.get(d, 0) + words - old["words"]
                if self.days[d] == 0:
                    del self.days[d]
            if old is None or old["words"] != words or old.get("mtime") != mtime:
                self.files[key] = {"words": words, "mtime": mtime}
                changed = True
        for key in [k for k in self.files if k not in keep]:
            del self.files[key]
            changed = True
        if changed:
            self.save()
        return changed

    def set_day(self, d: date, words: int):
        """Enter or correct a day by hand (e.g. days before tracking started)."""
        if words:
            self.days[d.isoformat()] = words
        else:
            self.days.pop(d.isoformat(), None)
        self.save()

    # ------------------------------------------------------------ reading
    def words_on(self, d: date) -> int:
        return self.days.get(d.isoformat(), 0)

    def first_day(self) -> date | None:
        return date.fromisoformat(min(self.days)) if self.days else None

    def streak(self, goal: int, now: float | None = None) -> int:
        """Consecutive days meeting the goal (any words if no goal), ending today, or yesterday
        when today hasn't got there yet."""
        need = max(goal, 1)
        d = self.today(now)
        if self.words_on(d) < need:
            d -= timedelta(days=1)
        n = 0
        while self.words_on(d) >= need:
            n += 1
            d -= timedelta(days=1)
        return n

    def best_day(self) -> tuple[date, int] | None:
        if not self.days:
            return None
        k = max(self.days, key=lambda k: self.days[k])
        return (date.fromisoformat(k), self.days[k]) if self.days[k] > 0 else None


def draft_counts(project) -> tuple[dict[str, tuple[int, float]], set[str]]:
    """({draft key: (words, mtime)} for drafts readable right now, {every linked draft key})."""
    out, linked = {}, set()
    for e in project.by_type("scene"):
        p = project.prose_path(e)
        if p is None:
            continue
        linked.add(project.prose_value(p))
        n = project.prose_words(e)
        if n is None:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        out[project.prose_value(p)] = (n, mtime)
    return out, linked
