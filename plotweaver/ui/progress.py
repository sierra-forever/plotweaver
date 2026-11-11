"""Progress tab: words written per day, the running total against the daily-goal pace,
streaks, and a way to enter days by hand."""
from __future__ import annotations

import calendar
import math
from datetime import date, timedelta

from PySide6.QtCore import QDate, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox, QToolTip, QVBoxLayout,
                               QWidget)

from ..wordlog import WordLog
from .theme import muted

RANGES = ["This month", "Last 7 days", "Last 30 days", "Last 90 days", "All time"]


def nice_step(span: float, ticks: int = 4) -> int:
    raw = max(span / ticks, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return max(int(m * mag), 1)
    return int(10 * mag)


def short_num(n: int) -> str:
    if abs(n) >= 10000:
        return f"{n / 1000:.0f}k"
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


class DayDialog(QDialog):
    """Enter or correct the words written on one day."""

    def __init__(self, log: WordLog, day: date, parent=None):
        super().__init__(parent)
        self.log = log
        self.setWindowTitle("Log words for a day")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.date = QDateEdit(QDate(day.year, day.month, day.day))
        self.date.setCalendarPopup(True)
        self.date.setMaximumDate(QDate(*log.today().timetuple()[:3]))
        self.date.setDisplayFormat("ddd d MMM yyyy")
        self.date.dateChanged.connect(self._load)
        self.words = QSpinBox()
        self.words.setRange(-1_000_000, 1_000_000)
        self.words.setSingleStep(100)
        self.words.setGroupSeparatorShown(True)
        form.addRow("Day", self.date)
        form.addRow("Words written", self.words)
        lay.addLayout(form)
        hint = QLabel("This replaces the day's count. For today, words you write after this "
                      "are still added on top.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load()

    def _day(self) -> date:
        q = self.date.date()
        return date(q.year(), q.month(), q.day())

    def _load(self, *_):
        self.words.setValue(self.log.words_on(self._day()))
        self.words.selectAll()

    def accept(self):
        self.log.set_day(self._day(), self.words.value())
        super().accept()


class ProgressChart(QWidget):
    """Two panels sharing the day axis: daily words (bars, goal line) above, running total
    (line, goal pace) below. One y-scale per panel."""
    dayActivated = Signal(object)  # date

    PAD_L, PAD_R, PAD_T, GAP, PAD_B = 52, 16, 10, 34, 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.days: list[date] = []
        self.values: list[int] = []
        self.today = date.today()
        self.goal = 0
        self.hover: int | None = None

    def set_data(self, days, values, today, goal):
        self.days, self.values, self.today, self.goal = days, values, today, goal
        self.hover = None
        self.update()

    # ------------------------------------------------------------ geometry
    def _panels(self) -> tuple[QRectF, QRectF]:
        w, h = self.width(), self.height()
        inner_h = h - self.PAD_T - self.GAP - self.PAD_B - 32
        top_h = inner_h * 0.55
        x0, x1 = self.PAD_L, w - self.PAD_R
        top = QRectF(x0, self.PAD_T + 16, x1 - x0, top_h)
        bottom = QRectF(x0, top.bottom() + self.GAP + 16, x1 - x0, inner_h - top_h)
        return top, bottom

    def _slot(self, rect: QRectF) -> float:
        return rect.width() / max(len(self.days), 1)

    def _index_at(self, x: float) -> int | None:
        top, _ = self._panels()
        if not self.days or x < top.left() or x > top.right():
            return None
        return min(int((x - top.left()) / self._slot(top)), len(self.days) - 1)

    def _cumulative(self) -> list[int]:
        out, run = [], 0
        for d, v in zip(self.days, self.values):
            run += v
            out.append(run)
        return out

    def _last_real(self) -> int:
        """Index of today, or the last day shown if the range is in the past."""
        n = 0
        for i, d in enumerate(self.days):
            if d <= self.today:
                n = i
        return n

    # ------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        ink, faint = pal.text().color(), muted(pal)
        accent = pal.highlight().color()
        grid = QColor(faint)
        grid.setAlpha(55)
        if not self.days:
            p.setPen(faint)
            p.drawText(self.rect(), Qt.AlignCenter, "No days to show.")
            return
        top, bottom = self._panels()
        f = QFont(self.font())
        small = QFont(f)
        small.setPointSizeF(f.pointSizeF() * 0.88)
        bold = QFont(f)
        bold.setBold(True)
        fm = QFontMetrics(small)
        slot = self._slot(top)
        last = self._last_real()

        # ---- top: daily words
        vals = self.values
        hi = max(max(vals, default=0), self.goal, 1)
        lo = min(min(vals, default=0), 0)
        step = nice_step(hi - lo)
        hi = math.ceil(hi * 1.08 / step) * step
        lo = math.floor(lo / step) * step if lo < 0 else 0

        def ty(v):
            return top.bottom() - (v - lo) / (hi - lo) * top.height()

        p.setFont(bold)
        p.setPen(ink)
        p.drawText(QPointF(top.left(), top.top() - 6), "Words per day")
        self._y_axis(p, top, lo, hi, step, ty, small, faint, grid)
        bar_w = max(min(slot - 2, 28), 1)
        for i, v in enumerate(vals):
            if v == 0:
                continue
            cx = top.left() + slot * (i + 0.5)
            y0, y1 = ty(0), ty(v)
            r = QRectF(cx - bar_w / 2, min(y0, y1), bar_w, abs(y1 - y0))
            c = QColor(accent) if v > 0 else QColor(faint)
            if 0 < v < self.goal:
                c.setAlpha(120)
            if i == self.hover:
                c = c.lighter(125)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            rad = min(3.0, bar_w / 2, r.height())
            path = QPainterPath()
            path.addRoundedRect(r, rad, rad)
            # square off the end that sits on the baseline
            if v > 0:
                path.addRect(QRectF(r.left(), r.bottom() - rad, r.width(), rad))
            else:
                path.addRect(QRectF(r.left(), r.top(), r.width(), rad))
            p.drawPath(path.simplified())
        p.setPen(QPen(faint, 1))
        p.drawLine(QPointF(top.left(), ty(0)), QPointF(top.right(), ty(0)))
        if self.goal:
            p.setPen(QPen(ink, 1, Qt.DashLine))
            gy = ty(self.goal)
            p.drawLine(QPointF(top.left(), gy), QPointF(top.right(), gy))
            p.setFont(small)
            label = f"goal {self.goal:,}"
            p.drawText(QPointF(top.right() - fm.horizontalAdvance(label) - 2, gy - 4), label)

        # ---- bottom: running total vs pace
        cum = self._cumulative()
        shown = cum[:last + 1]
        pace_end = self.goal * len(self.days)
        hi2 = max(max(shown, default=0), pace_end, 1)
        lo2 = min(min(shown, default=0), 0)
        step2 = nice_step(hi2 - lo2)
        hi2 = math.ceil(hi2 * 1.05 / step2) * step2
        lo2 = math.floor(lo2 / step2) * step2 if lo2 < 0 else 0

        def by(v):
            return bottom.bottom() - (v - lo2) / (hi2 - lo2) * bottom.height()

        p.setFont(bold)
        p.setPen(ink)
        p.drawText(QPointF(bottom.left(), bottom.top() - 6),
                   "Running total" + (" vs. goal pace" if self.goal else ""))
        self._y_axis(p, bottom, lo2, hi2, step2, by, small, faint, grid)
        p.setPen(QPen(faint, 1))
        p.drawLine(QPointF(bottom.left(), by(0)), QPointF(bottom.right(), by(0)))
        if self.goal:
            p.setPen(QPen(ink, 1, Qt.DashLine))
            p.drawLine(QPointF(bottom.left() + slot * 0.5, by(self.goal)),
                       QPointF(bottom.right() - slot * 0.5, by(pace_end)))
            p.setFont(small)
            label = f"pace → {pace_end:,}"
            p.drawText(QPointF(bottom.right() - fm.horizontalAdvance(label) - 2, by(pace_end) - 5), label)
        pts = [QPointF(bottom.left() + slot * (i + 0.5), by(v)) for i, v in enumerate(shown)]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        p.setPen(QPen(accent, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        end = pts[-1]
        p.setPen(QPen(pal.base().color(), 2))
        p.setBrush(accent)
        p.drawEllipse(end, 4.5, 4.5)
        p.setFont(small)
        p.setPen(ink)
        txt = f"{shown[-1]:,}" if shown else "0"
        tx = min(end.x() + 8, bottom.right() - fm.horizontalAdvance(txt))
        p.drawText(QPointF(tx, end.y() - 8), txt)

        # ---- x axis labels (under the bottom panel)
        p.setPen(faint)
        every = max(1, math.ceil(fm.horizontalAdvance("30 Nov") * 1.6 / slot))
        for i, d in enumerate(self.days):
            if i % every and d != self.today:
                continue
            if d == self.today and i % every:
                # don't let today's label collide with its neighbor
                if (i % every) < every * 0.6 or (every - i % every) < every * 0.6:
                    continue
            label = "Today" if d == self.today else f"{d.day} {d.strftime('%b')}"
            cx = bottom.left() + slot * (i + 0.5)
            wlab = fm.horizontalAdvance(label)
            p.drawText(QPointF(min(max(cx - wlab / 2, bottom.left()), bottom.right() - wlab),
                               bottom.bottom() + fm.ascent() + 6), label)

        # ---- hover crosshair
        if self.hover is not None:
            cx = top.left() + slot * (self.hover + 0.5)
            c = QColor(ink)
            c.setAlpha(70)
            p.setPen(QPen(c, 1))
            p.drawLine(QPointF(cx, top.top()), QPointF(cx, bottom.bottom()))
            if self.hover <= last:
                pt = QPointF(cx, by(cum[self.hover]))
                p.setPen(QPen(pal.base().color(), 2))
                p.setBrush(accent)
                p.drawEllipse(pt, 4, 4)
        p.end()

    @staticmethod
    def _y_axis(p, rect, lo, hi, step, ymap, font, faint, grid):
        p.setFont(font)
        fm = QFontMetrics(font)
        v = lo
        while v <= hi + 0.5:
            y = ymap(v)
            if v:
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(faint)
            s = short_num(int(v))
            p.drawText(QPointF(rect.left() - 8 - fm.horizontalAdvance(s), y + fm.ascent() / 2 - 1), s)
            v += step

    # ------------------------------------------------------------ interaction
    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().x())
        if i != self.hover:
            self.hover = i
            self.update()
        if i is None:
            QToolTip.hideText()
            return
        d = self.days[i]
        lines = [f"<b>{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}</b>"]
        if d > self.today:
            lines.append("(still to come)")
        else:
            v = self.values[i]
            lines.append(f"{v:,} words" + (" ✓" if self.goal and v >= self.goal else ""))
            total = self._cumulative()[i]
            lines.append(f"{total:,} in this range so far")
            if self.goal:
                diff = total - self.goal * (i + 1)
                lines.append(f"{abs(diff):,} {'ahead of' if diff >= 0 else 'behind'} pace")
        lines.append("<i>Double-click to edit this day</i>" if d <= self.today else "")
        QToolTip.showText(e.globalPosition().toPoint(), "<br>".join(x for x in lines if x), self)

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        i = self._index_at(e.position().x())
        if i is not None and self.days[i] <= self.today:
            self.dayActivated.emit(self.days[i])


class ProgressView(QWidget):
    selected = Signal(object)   # unused; same interface as the other views
    opened = Signal(object)
    changed = Signal()          # a day was edited by hand

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.log: WordLog | None = None
        self.goal = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 0, 8, 0)
        self.range = QComboBox()
        self.range.addItems(RANGES)
        self.range.setCurrentText(settings.value("progress/range", "This month", str))
        self.range.currentTextChanged.connect(self._range_changed)
        self.log_btn = QPushButton("Log words…")
        self.log_btn.setToolTip("Enter or correct a day's words, e.g. days before tracking started.")
        self.log_btn.clicked.connect(lambda: self.edit_day(self.log.today() if self.log else date.today()))
        bar.addWidget(QLabel("Show"))
        bar.addWidget(self.range)
        bar.addStretch(1)
        bar.addWidget(self.log_btn)
        lay.addLayout(bar)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setContentsMargins(10, 6, 10, 2)
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.chart = ProgressChart()
        self.chart.dayActivated.connect(self.edit_day)
        lay.addWidget(self.chart, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setContentsMargins(10, 0, 10, 6)
        self.note.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.note)

    def set_project(self, project, log: WordLog | None = None, goal: int = 0):
        self.project, self.log, self.goal = project, log, goal

    def set_goal(self, goal: int):
        self.goal = goal

    def set_query(self, q):
        pass

    def set_current(self, path):
        pass

    def _range_changed(self, t):
        self.settings.setValue("progress/range", t)
        self.refresh()

    def edit_day(self, d: date):
        if not self.log:
            return
        if DayDialog(self.log, d, self).exec():
            self.changed.emit()
            self.refresh()

    def _days(self) -> list[date]:
        today = self.log.today()
        r = self.range.currentText()
        if r == "This month":
            start = today.replace(day=1)
            end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        elif r == "All time":
            start = min(self.log.first_day() or today, today)
            end = today
        else:
            n = int(r.split()[1])
            start, end = today - timedelta(days=n - 1), today
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def refresh(self):
        if not self.log:
            return
        today = self.log.today()
        days = self._days()
        values = [self.log.words_on(d) if d <= today else 0 for d in days]
        self.chart.set_data(days, values, today, self.goal)
        self.log_btn.setEnabled(not self.log.error)

        past = [v for d, v in zip(days, values) if d <= today]
        total = sum(past)
        t = self.log.words_on(today)
        bits = [f"<b>Today:</b> {t:,}" + (f" / {self.goal:,}" if self.goal else "") +
                (" ✓" if self.goal and t >= self.goal else "")]
        bits.append(f"<b>{self.range.currentText()}:</b> {total:,}")
        if past:
            bits.append(f"<b>Daily average:</b> {round(total / len(past)):,}")
        streak = self.log.streak(self.goal)
        bits.append(f"<b>Streak:</b> {streak} day{'s' if streak != 1 else ''}"
                    + (" at goal" if self.goal else ""))
        best = self.log.best_day()
        if best:
            bits.append(f"<b>Best day:</b> {best[1]:,} ({best[0].day} {best[0].strftime('%b %Y')})")
        if self.goal and self.range.currentText() == "This month":
            left = days[-1] - today
            remaining = self.goal * len(days) - total
            if remaining > 0 and left.days >= 0:
                per = math.ceil(remaining / (left.days + 1))
                bits.append(f"<b>To hit pace by month's end:</b> {per:,}/day")
        self.summary.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(bits))

        if self.log.error:
            self.note.setText(self.log.error + " Tracking is paused so the log isn't overwritten.")
        elif not self.log.days:
            self.note.setText("Counting starts now: every time a linked draft is saved, the change in "
                              "its word count goes to that day. Use Log words… to fill in earlier days.")
        else:
            self.note.setText("" if self.goal else
                              "Set a Daily goal on the project note to see a goal line and pace.")
