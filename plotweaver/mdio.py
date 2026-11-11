"""Reading and writing Markdown files with YAML frontmatter (Obsidian style)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?", re.S | re.M)


class _Dumper(yaml.SafeDumper):
    """Block-style lists indented the way Obsidian writes them."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _str_rep(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_rep)


@dataclass
class Parsed:
    meta: dict
    body: str
    ok: bool = True        # False: the properties couldn't be read even after repairs
    repaired: list = field(default_factory=list)  # file line numbers we quoted to make it parse
    error: str = ""        # what went wrong, with the file line number


# `key: value` (optionally a list item `- key: value`) where value is unquoted plain text
_KV = re.compile(r"^(\s*(?:-\s+)?[^\s'\"#:\[\]{},][^:#]*?:[ \t]+)(\S.*?)\s*$")
_NO_QUOTE = tuple("'\"[{|>")
MAX_REPAIRS = 50


def _quote_line(line: str) -> str | None:
    """`note: Leading idea: she…` -> `note: 'Leading idea: she…'`. None if it can't help."""
    m = _KV.match(line)
    if not m or m.group(2).startswith(_NO_QUOTE):
        return None
    return m.group(1) + "'" + m.group(2).replace("'", "''") + "'"


def _load(fm: str):
    data = yaml.safe_load(fm)
    return {} if data is None else data


def parse_ex(text: str) -> Parsed:
    """Split a note into its properties and body.

    Hand-written properties often have an unquoted colon or a leading `*` or `@` in a value,
    which isn't valid YAML. Rather than refusing the whole note, each line YAML complains
    about is quoted as plain text and parsing is retried; `repaired` lists those lines. When
    that doesn't help, ok is False, meta is empty and body is the whole text, so nothing is
    lost."""
    text = text.lstrip("\ufeff")
    m = _FM.match(text)
    if not m:
        return Parsed({}, text)
    fm, body = m.group(1), text[m.end():]
    first_line = text[:m.start(1)].count("\n") + 1   # file line number of the block's first line
    lines = fm.split("\n")
    repaired = []
    for _ in range(MAX_REPAIRS):
        try:
            data = _load("\n".join(lines))
            break
        except yaml.YAMLError as ex:
            marks = [getattr(ex, "problem_mark", None), getattr(ex, "context_mark", None)]
            fixed = False
            for mk in marks:
                if mk is None or not (0 <= mk.line < len(lines)):
                    continue
                q = _quote_line(lines[mk.line])
                if q is not None and q != lines[mk.line]:
                    lines[mk.line] = q
                    repaired.append(first_line + mk.line)
                    fixed = True
                    break
            if not fixed:
                mk = marks[0] or marks[1]
                where = f"line {first_line + mk.line}: " if mk is not None else ""
                problem = getattr(ex, "problem", None) or str(ex).splitlines()[0]
                return Parsed({}, text, ok=False, error=where + problem)
    else:
        return Parsed({}, text, ok=False, error="too many problems to repair")
    if not isinstance(data, dict):
        return Parsed({}, text, ok=False, error="the properties block isn't a list of key: value lines")
    return Parsed(data, body, repaired=sorted(set(repaired)))


def parse(text: str):
    """Return (meta, body, ok). See parse_ex."""
    r = parse_ex(text)
    return r.meta, r.body, r.ok


def dump(meta: dict, body: str) -> str:
    if not meta:
        return body
    fm = yaml.dump(meta, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                   default_flow_style=False, width=4096)
    return "---\n" + fm + "---\n" + body


def dump_value(value) -> str:
    """One-line text form of a property value, for display in a table."""
    if isinstance(value, str):
        return value
    return yaml.dump(value, Dumper=_Dumper, default_flow_style=True, allow_unicode=True,
                     width=4096).strip().removesuffix("...").strip()


def load_value(text: str):
    """Inverse of dump_value, forgiving: falls back to the plain string."""
    t = text.strip()
    if not t:
        return ""
    if t.startswith("[["):
        return t
    try:
        v = yaml.safe_load(t)
    except yaml.YAMLError:
        return t
    return t if v is None else v
