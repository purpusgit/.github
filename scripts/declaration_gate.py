#!/usr/bin/env python3
"""The declaration gate (OFFLINE-01 spec: specs offline-first/declaration-ci-check.md, v1.4).

A pull request in a service repo that adds or changes a table or collection fails unless the same PR carries
that table's entry in declarations/data.yaml, complete. The gate reads the PR's diff and the files at its head.
It never reads a database and never runs repo code (a data.yaml or p4-collections.js is parsed as text).

    python3 scripts/declaration_gate.py --repo . --base-ref origin/main --mode report|block [--owners FILE]

Rules DC1 to DC10 are the spec's section 4. `report` always exits 0 (it writes the summary); `block` exits 1 on any
error. A crash exits 0 in report mode (with a warning) and 2 in block mode (fail closed).

Read the spec's section 3 for what counts as "touches table T" in each stack. Where the spec leaves a choice, the
choice is marked `# choice:` below and listed in the PR that introduced this file.
"""
import argparse
import bisect
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

try:
    import yaml
except ImportError:  # main() says so: report mode still passes, block mode fails closed
    yaml = None

LEVEL = {"DC1": "error", "DC2": "error", "DC3": "error", "DC4": "error", "DC5": "error", "DC6": "error",
         "DC7": "error", "DC8": "warning", "DC9": "warning", "DC10": "error"}
DISABLED = set()  # tests only: switch one rule off to prove its own test notices (never set in CI)

SCOPE_CLASSES = ("org", "person", "reference", "excluded")
PLACEMENTS = ("personal_or_content", "platform")
DEFAULT_EXCLUDE_PATHS = ["**/__tests__/**", "**/*.test.*", "test/**", "tests/**", "docs/**"]
DEFAULT_EXCLUDE_TABLES = ["schema_migrations", "subtleware_schema", "knex_migrations%", "databasechangelog%"]
CODE_EXT = (".sql", ".ts", ".js", ".go", ".py")
TENANT = "org_n"  # the reference tenant schema (spec section 3)
# English words that follow "CREATE TABLE" in prose; such a hit is DC9, never DC1.
STOPWORDS = set("""a an the it its this that these those to for if in on and or not are is as of by with from when then
only also first new old be can will must should may exists existing statement statements table tables here per""".split())

# ── DC5: names that look like secrets (whole parts split on "_") ──────────────────────────────────────────
SECRET_PARTS = {"password", "passwd", "secret", "token", "apikey", "otp", "hash", "salt", "credential", "ciphertext"}
SECRET_PAIRS = {("api", "key"), ("private", "key")}


def is_secret_like(name):
    parts = name.lower().split("_")
    if any(p in SECRET_PARTS for p in parts):
        return True
    if any(pair in SECRET_PAIRS for pair in zip(parts, parts[1:])):
        return True
    return len(parts) > 1 and parts[-1] in ("ct", "iv", "tag") or name.lower().startswith("key_")


# ── small helpers ────────────────────────────────────────────────────────────────────────────────────────
def glob_re(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += ".*"; i += 2
        elif pattern[i] == "*":
            out += "[^/]*"; i += 1
        elif pattern[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile("^" + out + "$")


def matches_any(path, patterns):
    return any(glob_re(p).match(path) for p in patterns)


def clean(text, limit=80):
    """Text from a PR, safe to print: no control characters, no workflow-command start, bounded."""
    return re.sub(r"[^A-Za-z0-9_.$@/ -]", "?", str(text))[:limit]


class Src:
    def __init__(self, path, text):
        self.path, self.text = path, text
        self._nl = [m.start() for m in re.finditer("\n", text)]

    def line(self, off):
        return bisect.bisect_left(self._nl, off) + 1


@dataclass
class Ev:
    table: object  # qualified lowercase name, or None when it could not be resolved
    op: str  # create | alter | index | drop | rename
    file: str
    line: int
    end: int
    cols: dict = field(default_factory=dict)  # column name -> line, the columns this statement adds
    drops: set = field(default_factory=set)
    raw: str = ""
    old: str = ""  # a rename: the table's old name
    note: str = ""  # set when only the unmasked scan found it


# ── plain SQL (also the engine under TS runners, Go, Drizzle, knex.raw and Liquibase sql: blocks) ───────────
# A bare name is placeholders or single characters, so there is only one way to split it (no regex backtracking blow-up
# on a long hostile identifier).
_ID = r'(?:`[^`\n]+`|"[^"\n]+"|\?\?|(?=[A-Za-z_{$])(?:\$\{[^}\n]*\}|\{\{[^}\n]*\}\}|[\w$])+)'
_QN = rf"{_ID}(?:[ \t]*\.[ \t]*{_ID})?"
_PRE = r"""(?P<pre>^[ \t]*|['"`][ \t]*)"""
RE_STMT = re.compile(rf"{_PRE}(?P<verb>CREATE|ALTER|DROP|RENAME)[ \t]+TABLE(?P<ine>[ \t]+IF[ \t]+(?:NOT[ \t]+)?EXISTS)?"
                     rf"(?:[ \t]+ONLY)?[ \t]+(?P<name>{_QN})", re.I | re.M)
RE_INDEX = re.compile(rf"{_PRE}(?:CREATE[ \t]+(?:UNIQUE[ \t]+|FULLTEXT[ \t]+|SPATIAL[ \t]+)?INDEX|DROP[ \t]+INDEX)[ \t]+"
                      rf"(?:CONCURRENTLY[ \t]+)?(?:IF[ \t]+(?:NOT[ \t]+)?EXISTS[ \t]+)?{_ID}(?:[ \t]+USING[ \t]+\w+)?"
                      rf"[ \t]+ON[ \t]+(?:ONLY[ \t]+)?(?P<name>{_QN})", re.I | re.M)
RE_USE = re.compile(r"^[ \t]*USE[ \t]+(" + _ID + r")[ \t]*;", re.I | re.M)
CONSTRAINT_WORDS = {"PRIMARY", "FOREIGN", "CONSTRAINT", "CHECK", "FULLTEXT", "SPATIAL", "EXCLUDE", "UNIQUE", "INDEX", "KEY",
                    "PARTITION", "LIKE", "PERIOD"}


def unquote(ident):
    ident = ident.strip()
    if ident[:1] in "`\"" and ident[-1:] == ident[:1]:
        ident = ident[1:-1]
    return ident


def mask_sql_comments(text):
    """Blank out `--`, `#` and `/* */` comments (same length, so offsets and lines survive). Quote aware."""
    out, i, n, q = list(text), 0, len(text), None
    while i < n:
        c = text[i]
        if q:
            if c == "\\" and q != "`":
                i += 2; continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c == "-" and text.startswith("--", i) and (i + 2 >= n or text[i + 2] in " \t\r\n"):
            j = text.find("\n", i); j = n if j < 0 else j
            out[i:j] = " " * (j - i); i = j; continue
        elif c == "#" and (i == 0 or text[i - 1] == "\n"):
            j = text.find("\n", i); j = n if j < 0 else j
            out[i:j] = " " * (j - i); i = j; continue
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]; i = j; continue
        i += 1
    return "".join(out)


RE_COMMENTED = re.compile(r"^[ \t]*--[ \t]*(?:CREATE|ALTER|DROP|RENAME)[ \t]+TABLE\b", re.I)


def reveal_record_blocks(text):
    """A commented `-- CREATE TABLE x (...)` record block counts (spec section 3): un-comment those lines."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if RE_COMMENTED.match(lines[i]):
            while i < len(lines) and lines[i].lstrip().startswith("--"):
                lines[i] = re.sub(r"--", "  ", lines[i], count=1)
                if ";" in lines[i]:
                    i += 1; break
                i += 1
            continue
        i += 1
    return "\n".join(lines)


def scan_to_end(text, start, quote=None):
    """Offset of the end of a statement: the closing quote for a quoted DDL string, else the first `;` outside
    quotes and parentheses. Bounded."""
    depth, i, n, q = 0, start, min(len(text), start + 40000), None
    while i < n:
        c = text[i]
        if q:
            if c == "\\" and q != "`":
                i += 2; continue
            if c == q:
                q = None
        elif quote and c == quote:
            return i
        elif c in "'\"`":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0 and not quote:
            return i
        i += 1
    return n


def split_top(text, base):
    """Split on commas outside parentheses and quotes. Returns [(offset, piece)]."""
    parts, depth, q, last = [], 0, None, 0
    for i, c in enumerate(text):
        if q:
            if c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append((base + last, text[last:i])); last = i + 1
    parts.append((base + last, text[last:]))
    return parts


class Resolver:
    """Turns a raw table name from code into one qualified name, or None."""

    def __init__(self, default_schema="", consts=None, tables_json=None):
        self.default_schema = default_schema.lower()
        self.consts = consts or {}
        self.tables_json = tables_json or {}

    def one(self, ident):
        ident = unquote(ident)

        def sub(m):
            inner = (m.group(1) or m.group(2)).strip()
            if re.search(r"schema|tenant", inner, re.I):
                return TENANT
            return self.consts.get(inner, "\0")  # "\0" marks a name that stays unresolved
        ident = re.sub(r"\$\{([^}]*)\}|\{\{([^}]*)\}\}", sub, ident)
        return None if (not ident or "\0" in ident or ident == "??") else ident.lower()

    def qualify(self, raw, use_schema=""):
        raw = raw.strip()
        m = re.match(rf"^({_ID})[ \t]*\.[ \t]*({_ID})$", raw)
        if m:
            a, b = self.one(m.group(1)), self.one(m.group(2))
            return f"{a}.{b}" if a and b else None
        t = self.one(raw)
        if t is None:
            return None
        schema = use_schema or self.default_schema
        return f"{schema}.{t}" if schema and "." not in t else t


# What may follow the table name in real DDL. Anything else is prose (`-- CREATE TABLE matches a from-empty build`).
AFTER_NAME = {
    "CREATE": {"(", "LIKE", "AS", "SELECT", ";", "ENGINE", "DEFAULT", "COMMENT", "CHARSET", "CHARACTER", "COLLATE", "PARTITION",
               "WITH", "INHERITS", "TABLESPACE", "USING", "ROW_FORMAT", "AUTO_INCREMENT", "", ")", "'", '"', "`"},
    "ALTER": {"ADD", "DROP", "MODIFY", "CHANGE", "RENAME", "ALTER", "ENGINE", "CONVERT", "AUTO_INCREMENT", "COMMENT", "DEFAULT",
              "CHARACTER", "CHARSET", "PARTITION", "ROW_FORMAT", "ALGORITHM", "LOCK", "ENABLE", "DISABLE", "ATTACH", "DETACH",
              "OWNER", "SET", "DISCARD", "IMPORT", "FORCE", "WITH", "WITHOUT", "VALIDATE", "COLLATE", "TABLESPACE"},
    "DROP": {",", ";", "CASCADE", "RESTRICT", "", ")", "'", '"', "`", "PURGE"},
    "RENAME": {"TO"},
}


def reads_like_ddl(verb, tail):
    m = re.match(r"\s*(\(|;|,|\)|['\"`]|[A-Za-z_]+|$)", tail)
    return m is not None and m.group(1).upper() in AFTER_NAME[verb]


def _sql_events(src, res, masked=True, uses_masked=False):
    text = src.text
    work = reveal_record_blocks(text)
    work = mask_sql_comments(work) if masked else light_strip(work) if uses_masked else work
    # `USE x;` inside a comment must not rename the tables after it, also in the unmasked scan (uses_masked)
    use_work = mask_sql_comments(reveal_record_blocks(text)) if uses_masked else work
    uses = [(m.start(), res.one(m.group(1))) for m in RE_USE.finditer(use_work)]

    def use_at(off):
        cur = ""
        for pos, name in uses:
            if pos <= off:
                cur = name or ""
        return cur
    evs = []
    for m in RE_STMT.finditer(work):
        verb, raw = m.group("verb").upper(), m.group("name")
        quote = m.group("pre").strip()[:1] or None
        start = m.start("verb")
        end = scan_to_end(work, m.end(), quote if quote in ("'", '"', "`") else None)
        body = work[m.end():end]
        use = use_at(start)
        bare = unquote(raw.split(".")[-1]) if "." not in raw else ""
        prose = "." not in raw and "${" not in raw and bare.lower() in STOPWORDS and not raw.startswith(("`", '"'))
        if prose or not reads_like_ddl(verb, work[m.end():m.end() + 60]):
            evs.append(Ev(None, "alter", src.path, src.line(start), src.line(end), raw="prose: " + raw)); continue
        table = res.qualify(raw, use)
        line0, line1 = src.line(start), src.line(end)
        if verb == "CREATE":
            cols = {}
            lm = re.match(r"\s*\(", body)
            if lm:
                inner_start = m.end() + lm.end()
                depth, q, j = 1, None, inner_start
                while j < len(work) and depth:
                    c = work[j]
                    if q:
                        if c == q:
                            q = None
                    elif c in "'\"`":
                        q = c
                    elif c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                    j += 1
                for off, piece in split_top(work[inner_start:j - 1], inner_start):
                    name = column_name(piece)
                    if name:
                        cols[name] = src.line(off + len(piece) - len(piece.lstrip()))
            evs.append(Ev(table, "create", src.path, line0, line1, cols, raw=raw))
        elif verb == "ALTER":
            evs.append(alter_event(src, res, work, m, end, table, raw, line0, line1))
        elif verb == "DROP":
            names = [raw] + [p.strip() for _, p in split_top(body, 0)[1:]]
            for k, nm in enumerate(names):
                nm = re.sub(r"^(?:IF[ \t]+EXISTS[ \t]+)", "", nm.strip(), flags=re.I).split()[0] if nm.strip() else ""
                if nm:
                    evs.append(Ev(res.qualify(nm, use), "drop", src.path, line0, line1, raw=nm))
        else:  # RENAME TABLE a TO b [, c TO d]
            pairs = [(raw, re.match(r"[ \t\r\n]*TO[ \t\r\n]+(" + _QN + ")", body, re.I))]
            for _, piece in split_top(body, 0)[1:]:
                pm = re.match(rf"[ \t\r\n]*({_QN})[ \t\r\n]+TO[ \t\r\n]+({_QN})", piece, re.I)
                if pm:
                    pairs.append((pm.group(1), re.match(r"(" + _QN + ")", pm.group(2))))
            for old, newm in pairs:
                if newm:
                    evs.append(Ev(res.qualify(newm.group(1), use), "rename", src.path, line0, line1, raw=newm.group(1),
                                  old=res.qualify(old, use) or ""))
                    evs.append(Ev(res.qualify(old, use), "drop", src.path, line0, line1, raw=old))
    for m in RE_INDEX.finditer(work):
        use = use_at(m.start())
        evs.append(Ev(res.qualify(m.group("name"), use), "index", src.path, src.line(m.start()), src.line(m.end()),
                      raw=m.group("name")))
    return evs


RE_DDLISH = re.compile(r"\b(?:CREATE|ALTER|DROP|RENAME)\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?(?:TABLE|INDEX)\b", re.I)


def light_strip(text):
    """The fail-safe scan's view of a file: only comments that cannot hide DDL are blanked. A `/* ... */` block that is closed, and
    a `-- ...` or `# ...` line comment, go (so commas and apostrophes in comments make no phantom columns), but never an
    unterminated block and never a comment that mentions CREATE/ALTER/DROP/RENAME TABLE or INDEX. It knows nothing about quotes,
    which is why it cannot be desynced by them."""
    def blank(m):
        return m.group(0) if RE_DDLISH.search(m.group(0)) else re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    return re.sub(r"(?m)(?:--(?=[ \t\r]|$)|^[ \t]*#).*$", blank, text)


FOUND_UNMASKED = "found outside the comment-masked pass"


def sql_events(src, res, masked=True):
    """DDL events of a file. The comment masker is a heuristic: a `$$ /* $$` string, `--don't`, a PostgreSQL `'C:\\'` or a MySQL
    `# it's` can desync it so that a `/*` swallows live DDL. So a masked file is also scanned a second way (light_strip, which
    knows nothing about quotes), and anything only that scan finds (a table, or a column of a table) counts as touched and is
    noted. A false positive (DDL that really sits in a comment) is accepted; a false pass is not."""
    evs = _sql_events(src, res, masked)
    if not masked:
        return evs
    have = {(e.op, e.table, e.raw, e.line): e for e in evs}
    for e in _sql_events(src, res, False, uses_masked=True):
        mine = have.get((e.op, e.table, e.raw, e.line))
        if mine is None:
            e.note = FOUND_UNMASKED
            evs.append(e)
        else:
            extra = {c: ln for c, ln in e.cols.items() if c not in mine.cols}
            if extra:
                mine.cols.update(extra)
                mine.note = FOUND_UNMASKED
    return evs


def column_name(piece):
    p = piece.strip()
    if not p:
        return None
    first = re.match(rf"({_ID})", p)
    if not first:
        return None
    word = first.group(1).upper()
    rest = p[first.end():].lstrip()
    if word in ("PRIMARY", "FOREIGN", "CONSTRAINT", "CHECK", "FULLTEXT", "SPATIAL", "EXCLUDE", "LIKE", "PERIOD"):
        return None
    if word in ("UNIQUE", "KEY", "INDEX"):
        after = re.sub(r"^(?:KEY|INDEX)\b\s*", "", rest, flags=re.I)
        after = re.sub(rf"^{_ID}\s*", "", after) if not after.startswith("(") else after
        if rest.startswith("(") or after.startswith("("):
            return None
    return unquote(first.group(1)).lower()


def alter_event(src, res, work, m, end, table, raw, line0, line1):
    body_start = m.end()
    ev = Ev(table, "alter", src.path, line0, line1, raw=raw)
    for off, clause in split_top(work[body_start:end], body_start):
        c = clause.strip()
        cline = src.line(off + len(clause) - len(clause.lstrip()))
        cm = re.match(rf"ADD[ \t\r\n]+(?:(COLUMN)[ \t\r\n]+)?(?:IF[ \t\r\n]+NOT[ \t\r\n]+EXISTS[ \t\r\n]+)?({_ID})(.*)$", c, re.I | re.S)
        if cm:
            word = unquote(cm.group(2)).upper()
            if not cm.group(1) and word in CONSTRAINT_WORDS:
                continue  # a new index or constraint: the table is touched, no column added
            ev.cols[unquote(cm.group(2)).lower()] = cline
            continue
        cm = re.match(rf"CHANGE[ \t\r\n]+(?:COLUMN[ \t\r\n]+)?({_ID})[ \t\r\n]+({_ID})", c, re.I)
        if cm:
            ev.drops.add(unquote(cm.group(1)).lower()); ev.cols[unquote(cm.group(2)).lower()] = cline; continue
        # RENAME [COLUMN] a TO b (PostgreSQL allows leaving COLUMN out); RENAME TO / CONSTRAINT / INDEX / KEY are not columns
        cm = re.match(rf"RENAME[ \t\r\n]+(?:COLUMN[ \t\r\n]+|(?!(?:TO|AS|COLUMN|CONSTRAINT|INDEX|KEY)\b))({_ID})[ \t\r\n]+TO[ \t\r\n]+({_ID})", c, re.I)
        if cm:
            ev.drops.add(unquote(cm.group(1)).lower()); ev.cols[unquote(cm.group(2)).lower()] = cline; continue
        cm = re.match(rf"DROP[ \t\r\n]+(?:COLUMN[ \t\r\n]+)?(?:IF[ \t\r\n]+EXISTS[ \t\r\n]+)?({_ID})", c, re.I)
        if cm and unquote(cm.group(1)).upper() not in CONSTRAINT_WORDS | {"CONSTRAINT"}:
            ev.drops.add(unquote(cm.group(1)).lower()); continue
        cm = re.match(rf"RENAME[ \t\r\n]+(?:TO|AS)[ \t\r\n]+({_QN})", c, re.I)
        if cm:
            ev.op, ev.old, ev.table = "rename", table or "", res.qualify(cm.group(1))
    return ev


# ── Liquibase YAML ───────────────────────────────────────────────────────────────────────────────────────
LB_CHANGES = ("createTable", "addColumn", "dropColumn", "renameColumn", "modifyDataType", "renameTable", "dropTable",
              "createIndex", "dropIndex", "addUniqueConstraint")


def _sval(node):
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _mget(node, key):
    """The value node of `key` in a mapping node, or None."""
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if _sval(k) == key:
                return v
    return None


def lb_entries(text):
    """Every (key node, value node) pair of a YAML changelog in document order, block or flow style, quoted keys too; None when
    the text is not YAML we can read (aliases are refused), so the caller falls back to the line scanner."""
    if yaml is None:
        return None
    try:
        docs = [d for d in yaml.compose_all(text, Loader=_no_alias_loader()) if d is not None]
    except (yaml.YAMLError, RecursionError):
        return None
    out, stack = [], list(reversed(docs))
    while stack:  # iterative: a deeply nested file cannot exhaust the stack
        n = stack.pop()
        if isinstance(n, yaml.MappingNode):
            out.extend(n.value)
            stack.extend(v for _, v in reversed(n.value))
        elif isinstance(n, yaml.SequenceNode):
            stack.extend(reversed(n.value))
    return out


def _lb_column_names(columns):
    """(name, line) of each column in a Liquibase `columns:` list, whether an item is {column: {name: x}} or {name: x}."""
    for item in (columns.value if isinstance(columns, yaml.SequenceNode) else []):
        node = _mget(item, "column") or item
        name = _mget(node, "name")
        if _sval(name):
            yield _sval(name).lower(), name.start_mark.line + 1


def liquibase_events_yaml(src, res, entries):
    evs = []
    for k, v in entries:
        kind = _sval(k)
        if kind not in LB_CHANGES or not isinstance(v, yaml.MappingNode):
            continue
        get = lambda key: _sval(_mget(v, key))  # noqa: E731
        tname, old, cols, drops = get("tableName"), "", {}, set()
        if kind in ("createTable", "addColumn"):
            cols = dict(_lb_column_names(_mget(v, "columns")))
        elif kind == "dropColumn":
            drops = {c for c, _ in _lb_column_names(_mget(v, "columns"))} | ({get("columnName").lower()} if get("columnName") else set())
        elif kind == "renameColumn":
            if get("oldColumnName"):
                drops.add(get("oldColumnName").lower())
            if get("newColumnName"):
                cols[get("newColumnName").lower()] = _mget(v, "newColumnName").start_mark.line + 1
        elif kind == "renameTable":
            old, tname = get("oldTableName") or tname, get("newTableName")
        schema = get("schemaName")
        qualify = lambda n: res.qualify(f"{schema}.{n}" if schema and "." not in n else n) if n else None  # noqa: E731
        op = {"createTable": "create", "dropTable": "drop", "renameTable": "rename", "createIndex": "index",
              "dropIndex": "index", "addUniqueConstraint": "index"}.get(kind, "alter")
        line = k.start_mark.line + 1
        end = max(line, v.end_mark.line + (0 if v.end_mark.column == 0 else 1))
        evs.append(Ev(qualify(tname), op, src.path, line, end, cols, drops, raw=tname or "", old=qualify(old) or ""))
    return evs


def lb_refs(text):
    """[(how, path)] of the files a changelog names: sqlFile path, include file, includeAll path. Read as YAML (block, flow,
    quoted) and also by line pattern; the union, so neither reader's blind spot is a way past."""
    refs = {(how, r) for how, rx in LB_REFS for r in re.findall(rx, text)}
    for k, v in lb_entries(text) or []:
        how = {"sqlFile": "sql", "include": "include", "includeAll": "all"}.get(_sval(k))
        ref = _sval(_mget(v, "file" if how == "include" else "path")) if how else None
        if ref:
            refs.add((how, ref))
    return sorted(refs)


def liquibase_events(src, res):
    entries = lb_entries(src.text)
    if entries is not None:
        return liquibase_events_yaml(src, res, entries) + sql_events(src, res, masked=False)
    return _liquibase_lines(src, res)


def _liquibase_lines(src, res):
    lines = src.text.split("\n")
    evs, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s*)?(" + "|".join(LB_CHANGES) + r"):\s*$", lines[i])
        if not m:
            i += 1; continue
        kind, indent, j = m.group(2), len(m.group(1)), i + 1
        while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent
                                  or lines[j].lstrip().startswith("- ") and len(lines[j]) - len(lines[j].lstrip()) > indent):
            j += 1
        block = lines[i + 1:j]
        tname = schema = None
        cols, drops = {}, set()
        for k, ln in enumerate(block):
            tm = re.match(r"\s*tableName:\s*['\"]?([\w$.${}]+)", ln)
            sm = re.match(r"\s*schemaName:\s*['\"]?([\w$]+)", ln)
            if tm and tname is None:
                tname = tm.group(1)
            if sm:
                schema = sm.group(1)
            nm = re.match(r"\s*-?\s*name:\s*['\"]?([\w$]+)", ln)
            if nm and kind in ("createTable", "addColumn"):
                cols[nm.group(1).lower()] = i + 2 + k
            cn = re.match(r"\s*(columnName|oldColumnName):\s*['\"]?([\w$]+)", ln)
            if cn and kind in ("dropColumn", "renameColumn", "modifyDataType"):
                drops.add(cn.group(2).lower()) if kind != "modifyDataType" else None
            nn = re.match(r"\s*newColumnName:\s*['\"]?([\w$]+)", ln)
            if nn:
                cols[nn.group(1).lower()] = i + 2 + k
            if kind == "renameTable":
                nt = re.match(r"\s*newTableName:\s*['\"]?([\w$]+)", ln)
                if nt:
                    cols = {}; tname, schema = nt.group(1), schema
        table = None
        if tname:
            table = res.qualify(f"{schema}.{tname}" if schema and "." not in tname else tname)
        op = {"createTable": "create", "dropTable": "drop", "renameTable": "rename", "createIndex": "index",
              "dropIndex": "index", "addUniqueConstraint": "index"}.get(kind, "alter")
        evs.append(Ev(table, op, src.path, i + 1, j, cols, drops, raw=tname or ""))
        i = j
    return evs + sql_events(src, res, masked=False)


# ── knex ─────────────────────────────────────────────────────────────────────────────────────────────────
# `schema.table(...)` is knex's alias of alterTable; `.withSchema('s')` may sit between `schema` and the call and names the schema
RE_KNEX = re.compile(r"\b(?:schema|knex\.schema)\s*\.\s*(?:withSchema\(\s*['\"](?P<ws>[\w$]+)['\"]\s*\)\s*\.\s*)?"
                     r"(?P<kind>createTable|alterTable|table|renameTable|dropTable|dropTableIfExists|createTableIfNotExists)\s*\(\s*(?P<arg>[^,)]+)")
KNEX_CALL = re.compile(r"\bschema\s*\.\s*(?:withSchema\([^)]*\)\s*\.\s*)?(?:createTable|alterTable|table|renameTable|dropTable|"
                       r"dropTableIfExists|createTableIfNotExists)\s*\(")
KNEX_NOT_COLS = {"dropcolumn", "dropcolumns", "dropindex", "index", "unique", "foreign", "primary", "dropforeign", "dropprimary",
                 "dropunique", "comment", "engine", "charset", "collate", "renamecolumn", "specifictype", "inherits"}


def knex_table(arg, res, schema=None):
    arg = arg.strip()
    name, lit = None, re.match(r"^['\"`]([^'\"`]+)['\"`]$", arg)
    if lit:
        name = lit.group(1)
    else:
        mem = re.match(r"^\w+(?:\.(\w+)|\[['\"](\w+)['\"]\])$", arg)
        if mem:
            key = mem.group(1) or mem.group(2)
            val = res.tables_json.get(key) or res.tables_json.get(key.upper()) or res.tables_json.get(key.lower())
            name = val if isinstance(val, str) else None
    if name is None:
        return None
    return res.qualify(f"{schema}.{name}" if schema and "." not in name else name)


def knex_builder_events(src, res):
    evs = []
    for m in RE_KNEX.finditer(src.text):
        kind, start = m.group("kind"), m.start()
        table = knex_table(m.group("arg"), res, m.group("ws"))
        depth, j, seen = 0, m.end(), False
        while j < len(src.text):
            c = src.text[j]
            if c == "{":
                depth += 1; seen = True
            elif c == "}":
                depth -= 1
                if seen and depth <= 0:
                    break
            elif c == ")" and not seen and kind in ("renameTable", "dropTable", "dropTableIfExists"):
                break
            j += 1
        body = src.text[m.end():j]
        cols = {}
        for cm in re.finditer(r"\b(?:table|t|tbl)\.(\w+)\(\s*['\"]([\w$]+)['\"]", body):
            if cm.group(1).lower() not in KNEX_NOT_COLS:
                cols[cm.group(2).lower()] = src.line(m.end() + cm.start())
        for cm in re.finditer(r"\b(?:table|t|tbl)\.(timestamps|timestamp)\s*\(\s*(?:true|\))", body):
            if cm.group(1) == "timestamps":
                for nm in ("created_at", "updated_at"):
                    cols[nm] = src.line(m.end() + cm.start())
        drops = {cm.group(1).lower() for cm in re.finditer(r"\.dropColumn\(\s*['\"]([\w$]+)", body)}
        op = {"createTable": "create", "createTableIfNotExists": "create", "alterTable": "alter", "table": "alter", "renameTable": "rename"}.get(kind, "drop")
        evs.append(Ev(table, op, src.path, src.line(start), src.line(j), cols, drops, raw=m.group("arg").strip()))
    return evs


def fix_placeholders(src, res, evs):
    """knex.raw DDL names its table `??`; the table is the first element of the call's array argument, resolved
    through tables.json."""
    for ev in evs:
        if ev.table is None and ev.raw == "??":
            start = sum(len(l) + 1 for l in src.text.split("\n")[:ev.line - 1])
            arr = re.search(r"\[\s*([^\],]+)", src.text[start:start + 800])
            ev.table = knex_table(arr.group(1), res) if arr else None
    return evs


def knex_events(src, res):
    return knex_builder_events(src, res) + fix_placeholders(src, res, sql_events(src, res, masked=False))


# ── Mongoose and the MongoDB driver ──────────────────────────────────────────────────────────────────────
_PLURAL = [(r"(m)an$", r"\1en"), (r"(pe)rson$", r"\1ople"), (r"(child)$", r"\1ren"), (r"^(ox)$", r"\1en"), (r"(ax|test)is$", r"\1es"),
           (r"(octop|vir)us$", r"\1i"), (r"(alias|status)$", r"\1es"), (r"(bu)s$", r"\1ses"), (r"(buffal|tomat|potat)o$", r"\1oes"),
           (r"([ti])um$", r"\1a"), (r"sis$", "ses"), (r"(?:([^f])fe|([lr])f)$", r"\1\2ves"), (r"(hive)$", r"\1s"),
           (r"([^aeiouy]|qu)y$", r"\1ies"), (r"(x|ch|ss|sh)$", r"\1es"), (r"(matr|vert|ind)ix|ex$", r"\1ices"),
           (r"([m|l])ouse$", r"\1ice"), (r"(kn|w|l)ife$", r"\1ives"), (r"(quiz)$", r"\1zes"), (r"s$", "s"), (r"([^a-z])$", r"\1"), (r"$", "s")]
_UNCOUNT = {"advice", "energy", "excretion", "digestion", "cooperation", "health", "justice", "labour", "machinery", "equipment",
            "information", "pollution", "sewage", "paper", "money", "species", "series", "rain", "rice", "fish", "sheep", "moose",
            "deer", "news", "data", "metadata"}


def pluralize(name):
    n = name.lower()
    if n in _UNCOUNT:
        return n
    for pat, rep in _PLURAL:
        if re.search(pat, n, re.I):
            return re.sub(pat, rep, n, count=1, flags=re.I)
    return n


FIELD_NOT = {"type", "ref", "required", "default", "enum", "index", "unique", "validate", "min", "max", "trim", "lowercase",
             "uppercase", "timestamps", "versionkey", "collection", "get", "set", "select", "sparse", "match", "minlength",
             "maxlength", "immutable", "alias", "of", "ttl", "expires", "strict", "autoindex", "autocreate", "_id", "id",
             "toobject", "tojson", "minimize", "writeconcern", "message", "validator", "sort", "populate"}
RE_FIELD = re.compile(r"^\s{2,}([A-Za-z_]\w*)\s*:\s*(?:\{|\[|String\b|Number\b|Boolean\b|Date\b|Buffer\b|Mixed\b|Map\b|"
                      r"Types\.|Schema\.Types|mongoose\.)")
RE_MODEL = re.compile(r"mongoose\s*\.\s*model\(\s*(?P<a>[^,]+?)\s*,\s*(?P<b>[A-Za-z_][\w.]*)\s*(?:,\s*['\"](?P<c>[^'\"]+)['\"])?\s*\)")
RE_COLL = re.compile(r"\.\s*(?:collection|createCollection)\(\s*['\"]([\w.$-]+)['\"]\s*\)")


def mongo_constants(files):
    for path, text in files.items():
        m = re.search(r"\bMongoCollections\s*=\s*(?:Object\.freeze\()?\{(.*?)\n\}", text, re.S)
        if m:
            return {k: v for k, v in re.findall(r"^\s*(\w+)\s*:\s*['\"]([\w.$-]+)['\"]", m.group(1), re.M)}, path
    return {}, None


def mongo_model_map(files, default_schema):
    """(schema-file path -> qualified collection names, all collection names, call site -> collection), from every
    mongoose.model(...) call in the repo."""
    consts, _ = mongo_constants(files)
    out, models, sites = {}, [], {}
    for path, text in files.items():
        if not path.endswith((".js", ".ts")):
            continue
        for m in RE_MODEL.finditer(text):
            a, b, c = m.group("a").strip(), m.group("b"), m.group("c")
            lit = re.match(r"^['\"]([^'\"]+)['\"]$", a)
            mem = re.match(r"^\w+\.(\w+)$", a)
            name = lit.group(1) if lit else consts.get(mem.group(1)) if mem else None
            target = resolve_schema_file(files, path, text, b)
            opt = None
            if target:
                om = re.search(r"collection\s*:\s*['\"]([\w.$-]+)['\"]", files[target])
                opt = om.group(1) if om else None
            coll = c or opt or (pluralize(name) if name else None)
            if coll:
                q = f"{default_schema}.{coll}".lower() if default_schema else coll.lower()
                models.append(q)
                sites[(path, m.start())] = q
                out.setdefault(target or path, set()).add(q)
    return out, set(models), sites


def resolve_schema_file(files, path, text, ident):
    head = ident.split(".")[0]
    if re.search(rf"\b(?:const|let|var)\s+{re.escape(head)}\s*=\s*new\s+(?:mongoose\.)?Schema\b", text):
        return path
    m = re.search(rf"\b(?:const|let|var)\s+(?:{re.escape(head)}|\{{[^}}]*\b{re.escape(head)}\b[^}}]*\}})\s*=\s*require\(\s*['\"]([^'\"]+)['\"]", text)
    if not m:
        return None
    base = os.path.normpath(os.path.join(os.path.dirname(path), m.group(1)))
    for cand in (base, base + ".js", base + ".ts", os.path.join(base, "index.js")):
        if cand in files:
            return cand
    return None


def mongoose_events(src, res, ctx):
    evs = []
    if src.path.endswith(".indexes.js"):
        stem = os.path.basename(src.path)[:-len(".indexes.js")]
        evs.append(Ev(res.qualify(stem), "index", src.path, 1, src.text.count("\n") + 1, raw=stem))
    # field changes in a schema file, mapped to the collection(s) its model registers
    fields = {}
    for m in re.finditer(r"(?m)^.*$", src.text):
        fm = RE_FIELD.match(m.group(0))
        if fm and fm.group(1).lower() not in FIELD_NOT:
            fields.setdefault(fm.group(1), src.line(m.start()))
    if fields:
        for coll in sorted(ctx.get("model_map", {}).get(src.path, set())) or [None]:
            evs.append(Ev(coll, "alter", src.path, min(fields.values()), max(fields.values()), dict(fields),
                          raw=os.path.basename(src.path)))
    # a new MongoCollections constant is a new collection name
    cm = re.search(r"\bMongoCollections\s*=\s*(?:Object\.freeze\()?\{(.*?)\n\}", src.text, re.S)
    if cm:
        for m in re.finditer(r"(?m)^\s*(\w+)\s*:\s*['\"]([\w.$-]+)['\"]", cm.group(1)):
            ln = src.line(cm.start(1) + m.start())
            evs.append(Ev(res.qualify(m.group(2)), "create", src.path, ln, ln, raw=m.group(2)))
    # mongoose.model(...) itself: a new or changed model
    for m in RE_MODEL.finditer(src.text):
        coll = ctx.get("model_sites", {}).get((src.path, m.start()))
        if coll:
            evs.append(Ev(coll, "create", src.path, src.line(m.start()), src.line(m.end()), raw=coll))
    return evs


def driver_events(src, res, ctx):
    evs = []
    for m in RE_COLL.finditer(src.text):
        table = res.qualify(m.group(1))
        line_text = src.text[src.text.rfind("\n", 0, m.start()) + 1:src.text.find("\n", m.end()) if "\n" in src.text[m.end():] else len(src.text)]
        known = table in ctx.get("known", set())
        if not known or re.search(r"createIndex|createCollection|ensureIndex", line_text):
            evs.append(Ev(table, "create" if not known else "index", src.path, src.line(m.start()), src.line(m.end()), raw=m.group(1)))
    return evs


# ── per-file dispatch ────────────────────────────────────────────────────────────────────────────────────
def extract(src, kind, res, ctx):
    p = src.path.lower()
    if kind in ("sql", "drizzle", "go"):
        return sql_events(src, res, masked=p.endswith(".sql"))
    if kind == "tsdl":
        return sql_events(src, res, masked=False)
    if kind == "liquibase":
        return liquibase_events(src, res) if p.endswith((".yaml", ".yml")) else sql_events(src, res, masked=p.endswith(".sql"))
    if kind == "knex":
        return knex_events(src, res)
    if kind == "mongoose":
        return mongoose_events(src, res, ctx) + driver_events(src, res, ctx)
    if kind == "mongodriver":
        return driver_events(src, res, ctx)
    # auto: by extension and content
    if p.endswith(".sql"):
        return sql_events(src, res, masked=True)
    if p.endswith((".yaml", ".yml")):
        return liquibase_events(src, res) if "databaseChangeLog" in src.text else []
    if p.endswith((".go", ".py")):
        return sql_events(src, res, masked=False)
    if p.endswith((".ts", ".js")):
        evs = fix_placeholders(src, res, sql_events(src, res, masked=False))
        if RE_KNEX.search(src.text):
            evs += knex_builder_events(src, res)
        return evs + mongoose_events(src, res, ctx) + driver_events(src, res, ctx)
    return []


# ── git ──────────────────────────────────────────────────────────────────────────────────────────────────
class GateError(Exception):
    """An expected failure that is the PR's (or the caller's) to fix, not a bug in the gate."""


def git(repo, *args, soft=False):
    r = subprocess.run(["git", "-c", "core.quotepath=off", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode:
        if soft:
            return None
        raise GateError(f"git {args[0]} failed: {clean(r.stderr.strip(), 200)}")
    return r.stdout


class Blobs:
    """Reads files of one commit through a single `git cat-file --batch` process: the gate judges what is committed,
    not what happens to be on disk (a sparse or dirty checkout would otherwise change the verdict)."""

    def __init__(self, repo, rev):
        self.rev = rev
        self.proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.cache = {}

    def get(self, path):
        if path in self.cache or "\n" in path:
            return self.cache.get(path)
        self.proc.stdin.write(f"{self.rev}:{path}\n".encode())
        self.proc.stdin.flush()
        head = self.proc.stdout.readline().split()
        text = None
        if len(head) == 3:  # "<sha> <type> <size>"; "<name> missing" has two parts. Read every object to its end:
            data = self.proc.stdout.read(int(head[2]))  # a gitlink is a commit, and an unread body shifts every later answer
            self.proc.stdout.read(1)
            if head[1] == b"blob":
                text = data.decode("utf-8-sig", "replace") if len(data) < 2_000_000 else None  # -sig: drop a BOM
        self.cache[path] = text
        return text

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()
        self.proc.stdout.close()


def added_lines(repo, mb):
    """{path: set of added line numbers} for the PR. File names come from `--name-only -z` (NUL-separated, never quoted); the
    hunks are read one file at a time, so no patch header is parsed (git C-quotes `"` and `\\` in them and tab-pads names with
    spaces). --text: a PR's .gitattributes (`*.sql -diff`) must not turn the diff into "Binary files differ"; --no-renames: a
    file moved out of exclude_paths into schema_paths must count as added; no textconv or external diff driver."""
    flags = ["--no-color", "--text", "--no-textconv", "--no-ext-diff", "--no-renames"]
    files = {}
    for path in git(repo, "diff", "--name-only", "-z", *flags, mb, "HEAD").split("\0"):
        if not path:
            continue
        files[path] = lines = set()
        for ln in git(repo, "diff", "--unified=0", *flags, mb, "HEAD", "--", ":(literal)" + path).splitlines():
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
            if m:
                start, n = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
                lines.update(range(start, start + n))
    return files


def _no_alias_loader():
    """data.yaml and owners.yaml are read from a PR: no aliases, so a file cannot expand itself into gigabytes."""
    class NoAliasLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                raise yaml.YAMLError("aliases are not allowed")
            return super().compose_node(parent, index)
    return NoAliasLoader


def load_yaml(text):
    return yaml.load(text, Loader=_no_alias_loader())  # noqa: S506 - a SafeLoader subclass


# ── config (declarations/data.yaml, or social's interim p4-collections.js through an adapter) ───────────────
DATA = "declarations/data.yaml"
P4 = "declarations/p4-collections.js"
SOCIAL_PATHS = [{"glob": g, "kind": "mongoose"} for g in (
    "orbit-social/models/schema/**/*.js", "models/*.js", "orbit-social/utils.js", "models/indexes/*.indexes.js")]
EVERYTHING = {"schema_paths": [{"glob": "**", "kind": "auto"}], "exclude_paths": [], "exclude_tables": []}


def js_object_keys(body):
    """Top-level keys of a JS object literal's inner text: skips strings, nested braces and brackets."""
    keys, i, n, depth = [], 0, len(body), 0
    while i < n:
        c = body[i]
        if c in "'\"`":
            j = i + 1
            while j < n and body[j] != c:
                j += 2 if body[j] == "\\" else 1
            i = j + 1
            continue
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif depth == 0 and (c.isalpha() or c == "_"):
            m = re.match(r"([A-Za-z_]\w*)\s*:", body[i:])
            if m:
                keys.append(m.group(1)); i += m.end(); continue
            i += re.match(r"\w+", body[i:]).end(); continue
        i += 1
    return keys


def p4_adapter(text):
    """Read p4-collections.js as text (never run it). A shape the adapter does not know yields no entries."""
    svc = re.search(r"service\s*:\s*['\"]([\w-]+)['\"]", text)
    service = svc.group(1) if svc else ""
    marks = [m for m in re.finditer(r"collection\s*:\s*['\"]([\w.$-]+)['\"]", text)]
    tables = []
    for k, m in enumerate(marks):
        block = text[m.start():marks[k + 1].start() if k + 1 < len(marks) else len(text)]

        def lit(key):
            lm = re.search(rf"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", block, re.S)
            return lm.group(2) if lm else None
        fm = re.search(r"\bfields\s*:\s*\{", block)
        fields = []
        if fm:
            depth, j = 1, fm.end()
            while j < len(block) and depth:
                depth += (block[j] == "{") - (block[j] == "}"); j += 1
            fields = js_object_keys(block[fm.end():j - 1])
        rp = re.search(r"\breaches_phones\s*:\s*(true|false)", block)
        pf = re.search(r"\bphone_fields\s*:\s*\[([^\]]*)\]", block)
        phone = [x.strip(" '\"") for x in pf.group(1).split(",") if x.strip()] if pf else (None if rp and rp.group(1) == "true" else [])
        tables.append({"table": f"{service}.{m.group(1)}" if service else m.group(1), "scope_class": lit("scope_class"),
                       "placement": lit("placement"), "placement_key": lit("placement_key"), "meaning": lit("meaning"),
                       "fields": fields, "phone_fields": phone, "secret_like": {}})
    return {"version": 1, "default_schema": service, "schema_paths": SOCIAL_PATHS, "tables": tables}


def load_cfg(read):
    """read(path) -> text or None. Returns the parsed config, or None when the tree carries none."""
    text = read(DATA)
    if text is not None:
        try:
            cfg = load_yaml(text)
        except yaml.YAMLError:
            raise GateError(f"{DATA} is not valid YAML")
        if not isinstance(cfg, dict):
            raise GateError(f"{DATA} must be a mapping with a `tables:` list")
        return cfg
    p4 = read(P4)
    return p4_adapter(p4) if p4 is not None else None


def norm_table(name, default_schema):
    n = str(name).strip().strip("`\"").lower()
    return n if "." in n or not default_schema else f"{default_schema.lower()}.{n}"


def canon_repo(name):
    """The same repo under its different spellings: `purpusgit/service_orbit_orgs`, `service_orbit_orgs` and `orbit_orgs`
    (owners.yaml names repos without the `service_` prefix) all become `orbit_orgs`."""
    n = str(name or "").strip().lower().rsplit("/", 1)[-1]
    return n[len("service_"):] if n.startswith("service_") else n


def load_owners(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        d = load_yaml(fh.read()) or {}
    d = d.get("owners", d.get("tables", d)) if isinstance(d, dict) else d
    if isinstance(d, list):
        return {str(i.get("table", "")).lower(): str(i.get("owner", "")) for i in d if isinstance(i, dict)}
    return {str(k).lower(): str(v) for k, v in d.items()} if isinstance(d, dict) else {}


# ── report ───────────────────────────────────────────────────────────────────────────────────────────────
class Report:
    def __init__(self, mode):
        self.mode, self.findings = mode, []
        self.touched, self.added, self.secret_new, self.excluded, self.foreign, self.inventory, self.notes = {}, {}, [], [], [], [], []
        self.removable, self.mongo, self.unmasked = [], False, []  # dropped tables whose entry can go; a Mongo inventory caveat; tables only the unmasked scan found

    def add(self, rule, table, msg, file=None, line=None, level=None):
        if rule in DISABLED:
            return
        self.findings.append({"rule": rule, "level": level or LEVEL[rule], "table": table, "msg": msg, "file": file, "line": line})

    @property
    def errors(self):
        return [f for f in self.findings if f["level"] == "error"]


def decision(secret_like, col):
    """'exclude', 'keep' (with a reason) or None for a column in an entry's secret_like map."""
    v = next((x for k, x in (secret_like or {}).items() if str(k).lower() == col.lower()), None)
    if isinstance(v, str):
        if v.strip().lower() == "exclude":
            return "exclude"
        m = re.match(r"keep\b[\s:,-]*(.*)$", v.strip(), re.I)
        return "keep" if m and m.group(1).strip() else None
    if isinstance(v, dict):
        d = str(v.get("decision", "keep" if "keep" in v else "")).lower()
        reason = v.get("reason") or (v.get("keep") if isinstance(v.get("keep"), str) else "")
        return "exclude" if d == "exclude" else "keep" if d == "keep" and str(reason).strip() else None
    return None


def build_knowledge(all_events):
    """Tables created in the repo, and each one's columns after all files are read: the union of adds, minus columns
    dropped and not added again later (file numbers do not follow merge order, so the order is by path and line).
    A rename makes the new name a table of this repo and carries the old name's columns over."""
    order = sorted((ev for evs in all_events.values() for ev in evs if ev.table), key=lambda e: (e.file, e.line))
    created, cols, dropped = set(), {}, {}
    for pos, ev in enumerate(order):
        t = ev.table
        if ev.op in ("create", "rename"):
            created.add(t)
        if ev.op == "rename" and ev.old:
            cols.setdefault(t, {}).update(cols.get(ev.old, {}))
        if ev.op in ("create", "alter", "rename"):
            for c, ln in ev.cols.items():
                cols.setdefault(t, {})[c] = (ev.file, ln)
                dropped.get(t, {}).pop(c, None)
        for c in ev.drops:
            dropped.setdefault(t, {})[c] = pos
    for t, d in dropped.items():
        for c in d:
            cols.get(t, {}).pop(c, None)
    return created, cols


MONGO_IMPLICIT = {"_id", "__v", "createdat", "updatedat"}


def tree_files(repo, rev):
    """(paths, symlink paths) of a commit. `-z` keeps odd names intact; mode 120000 is a symlink, whose blob is its target string."""
    paths, links = [], set()
    for ent in git(repo, "ls-tree", "-r", "-z", rev).split("\0"):
        if ent:
            meta, path = ent.split("\t", 1)
            paths.append(path)
            if meta.startswith("120000"):
                links.add(path)
    return paths, links


# Files a Liquibase changelog names. They are schema files wherever they live, even under exclude_paths (docs/, tests/):
# that is where DDL would otherwise hide.
LB_REFS = (("sql", r"sqlFile:\s*\n(?:\s+\w+:.*\n)*?\s+path:\s*['\"]?([\w./-]+)"),
           ("include", r"include:\s*\n(?:\s+\w+:.*\n)*?\s+file:\s*['\"]?([\w./-]+)"),
           ("all", r"includeAll:\s*\n(?:\s+\w+:.*\n)*?\s+path:\s*['\"]?([\w./-]+)"))


class Tree:
    """What the gate knows about one tree (the PR's head, or its merge base): its schema files, the events in them,
    the tables created and each table's columns."""

    def __init__(self, tracked, links, text_of, kind_of, dflt, known_names, exclude_paths):
        # a symlink's content is its target's name: it is never read as a schema file, and the caller reports it
        self.symlinks = sorted(p for p in tracked if p in links and kind_of(p))
        self.schema_files = {p: kind_of(p) for p in tracked if kind_of(p) and p not in links and text_of(p) is not None}
        code = {p: text_of(p) for p in tracked if p.endswith((".js", ".ts")) and p not in links and not matches_any(p, exclude_paths)
                and text_of(p) is not None}
        consts = {}
        for t in code.values():
            for k, v in re.findall(r"export\s+const\s+(\w+)\s*=\s*['\"`]([^'\"`$\n]+)['\"`]", t):
                consts[k] = v
        tj, self.notes = {}, []
        for p in tracked:
            if p.endswith("migrations/constant/tables.json") and text_of(p):
                try:
                    tj.update({k: v for k, v in json.loads(text_of(p)).items() if isinstance(v, str)})
                except ValueError:
                    self.notes.append(f"{p} is not valid JSON; knex table names are unresolved")
        self.res = res = Resolver(dflt, consts, tj)
        model_map, self.model_names, model_sites = mongo_model_map(code, dflt)
        ctx = {"model_map": model_map, "model_sites": model_sites, "known": set(known_names) | self.model_names}
        self.events = {p: extract(Src(p, text_of(p)), kind, res, ctx) for p, kind in self.schema_files.items()}
        # Liquibase `sqlFile`, `include` and `includeAll` name files wherever they live: read them too, followed transitively
        tracked_set, queue, budget = set(tracked), [q for q in self.schema_files if q.endswith((".yaml", ".yml"))], 500
        while queue and budget:
            budget -= 1
            p = queue.pop()
            for how, ref in lb_refs(text_of(p) or ""):
                for c in (os.path.normpath(os.path.join(os.path.dirname(p), ref)), os.path.normpath(ref)):
                    found = [f for f in tracked if f.startswith(c + "/")] if how == "all" and c != "." else \
                        [c] if c in tracked_set else []
                    found = [f for f in found if f not in self.events and f not in links and text_of(f) is not None
                             and (how == "sql" or f.endswith((".yaml", ".yml", ".sql")))]
                    for f in found:
                        kind = "liquibase" if f.endswith((".yaml", ".yml")) else "sql"
                        self.schema_files[f] = kind
                        self.events[f] = extract(Src(f, text_of(f)), kind, res, ctx)
                        if kind == "liquibase":
                            queue.append(f)
                    if found:
                        break
        self.created, self.cols = build_knowledge(self.events)
        self.created |= self.model_names
        for p, colls in model_map.items():
            text = text_of(p) or ""
            for coll in colls:
                for i, line in enumerate(text.split("\n"), 1):
                    fm = RE_FIELD.match(line)
                    if fm and fm.group(1).lower() not in FIELD_NOT:
                        self.cols.setdefault(coll, {}).setdefault(fm.group(1), (p, i))


def judge(repo, base_ref, mode, owners_path=None, calling_repo=None):
    mb = git(repo, "merge-base", base_ref, "HEAD").strip()
    hb, bb = Blobs(repo, "HEAD"), Blobs(repo, mb)
    try:
        return judge_trees(repo, mode, owners_path, mb, hb.get, bb.get, calling_repo)
    finally:
        hb.close(); bb.close()


def judge_trees(repo, mode, owners_path, mb, head_text, base_text, calling_repo=None):
    rep = Report(mode)
    added = added_lines(repo, mb)
    tracked, links = tree_files(repo, "HEAD")
    base_tracked, base_links = tree_files(repo, mb)

    head = load_cfg(head_text)
    base = load_cfg(base_text)
    cfg_in = base if base is not None else EVERYTHING
    schema_paths = [{"glob": sp, "kind": "auto"} if isinstance(sp, str) else sp
                    for sp in (cfg_in.get("schema_paths") or EVERYTHING["schema_paths"])]
    exclude_paths = DEFAULT_EXCLUDE_PATHS + [p for p in (cfg_in.get("exclude_paths") or []) if isinstance(p, str)]
    exclude_tables = DEFAULT_EXCLUDE_TABLES + [str(p).lower() for p in (cfg_in.get("exclude_tables") or [])]
    hc = head or {}
    # like the other DC8 keys: judged from the base copy (the head's only when the PR creates data.yaml)
    dflt = str((cfg_in if base is not None else hc).get("default_schema") or "").lower()

    def entries_of(cfg):
        return {norm_table(e["table"], dflt): e for e in ((cfg or {}).get("tables") or []) if isinstance(e, dict) and e.get("table")}
    entries, base_entries = entries_of(hc), entries_of(base)
    foreign_decl = {norm_table(f["table"], dflt): f for f in (hc.get("foreign_changes") or []) if isinstance(f, dict) and f.get("table")}

    def excluded_table(t):
        bare = t.split(".")[-1]
        return any(fnmatch.fnmatchcase(x, pat.replace("%", "*")) for x in (t, bare) for pat in exclude_tables)

    # DC8: schema_paths, exclude_paths, exclude_tables and foreign_changes
    if head is not None and base is None:
        rep.add("DC8", "-", f"{DATA} is created by this PR, so every path in the repo is judged as schema_paths", DATA)
    elif head is not None and base is not None:
        for key in ("schema_paths", "exclude_paths", "exclude_tables", "default_schema", "foreign_changes"):
            if json.dumps(head.get(key), sort_keys=True, default=str) != json.dumps(base.get(key), sort_keys=True, default=str):
                # choice: foreign_changes is read from the PR's head so a PR can list its own foreign change (DC7 a);
                # the other three are judged against the base copy, as the spec says.
                rep.add("DC8", "-", f"this PR changes `{key}`; " + ("the base copy is used for judging" if key != "foreign_changes"
                        else "the PR's copy is used, and the change is named here for review"), DATA)

    def kind_of(path):
        if matches_any(path, exclude_paths):
            return None
        for sp in schema_paths:
            if glob_re(sp.get("glob", "")).match(path):
                return sp.get("kind", "auto")
        return None

    known = set(base_entries)  # a collection name is "new" when the base did not know it
    new = Tree(tracked, links, head_text, kind_of, dflt, known, exclude_paths)
    old = Tree(base_tracked, base_links, base_text, kind_of, dflt, known, exclude_paths)
    rep.notes += new.notes
    for p in new.symlinks:
        rep.add("DC9", "-", "schema files must not be symlinks: a link's content is its target, which is not judged here", p,
                level="error" if mode == "block" else None)

    for p in sorted(added):
        if added[p] and kind_of(p) and head_text(p) is None:
            rep.add("DC9", "-", "schema path changed but the file could not be read (over 2 MB?); its tables are not judged", p,
                    level="error" if mode == "block" else None)  # block: an unjudged schema file must not pass

    # ── what this PR touches ─────────────────────────────────────────────────────────────────────────────
    touched = {}
    for p, evs in new.events.items():
        lines = added.get(p)
        if not lines:
            continue
        hit = False
        for ev in evs:
            if not any(ln in lines for ln in range(ev.line, ev.end + 1)):
                continue
            hit = True
            if ev.table is None:
                rep.add("DC9", ev.raw, f"could not resolve the table name `{clean(ev.raw)}`", p, ev.line)
                continue
            if ev.note and ev.table not in rep.unmasked:
                rep.unmasked.append(ev.table)
            t = touched.setdefault(ev.table, {"ops": set(), "file": p, "line": ev.line})
            t["ops"].add(ev.op)
        # "auto" is the judged-by-everything mode of a PR that creates data.yaml: most files hold no schema, so no warning
        if not hit and new.schema_files[p] != "auto" and p not in (DATA, P4):
            rep.add("DC9", "-", "schema path changed, no table found in the changed lines", p, min(lines))

    # ── the rules, table by table ────────────────────────────────────────────────────────────────────────
    owners = load_owners(owners_path)
    me = canon_repo(calling_repo)
    for t in sorted(touched):
        info = touched[t]
        if excluded_table(t):
            rep.excluded.append(t)
            continue
        rep.touched[t] = sorted(info["ops"])
        # the columns this PR adds: those the table has at the head and did not have at the merge base
        added_cols = {c: ln for c, (f, ln) in new.cols.get(t, {}).items() if c not in old.cols.get(t, {})}
        rep.added[t] = sorted(added_cols)
        if info["ops"] <= {"drop"}:
            if t in entries:
                rep.removable.append(t)  # a note in the summary, never an error
            continue  # choice: a dropped table needs no entry; the summary lists it
        entry, where = entries.get(t), (info["file"], info["line"])
        if entry is None and t not in new.created:
            f = foreign_decl.get(t)
            if me and me in (canon_repo((owners or {}).get(t)), canon_repo((f or {}).get("owner"))):
                # the calling repo owns this table (owners.yaml) or the item says it does: not a foreign change, so no
                # shortcut past DC2 to DC6. It needs an entry like any table of its own.
                if f is not None:
                    rep.add("DC7", t, "is listed under foreign_changes, but this repo owns the table (owners.yaml, or the "
                            "item's owner): a foreign change names another repo. Give the table an entry instead.", *where)
                rep.add("DC1", t, "is changed by this repo, which owns it, and has no entry in " + DATA, *where)
                continue
            rep.foreign.append({"table": t, "owner": (f or {}).get("owner"), "cols": sorted(added_cols)})
            if f is None:
                rep.add("DC7", t, "changes a table this repo does not create and has no entry for; list it under "
                        "`foreign_changes:` with its owner", *where)
                continue
            if mode == "block":
                if owners is None:
                    rep.add("DC7", t, "owner not checked: declarations/owners.yaml does not exist yet", *where, level="warning")
                elif t not in owners:
                    rep.add("DC7", t, "is not listed in declarations/owners.yaml, so its owner cannot be checked; ask GIT-01 to "
                            "regenerate the file with this table and its owner", *where)
                elif canon_repo(owners[t]) != canon_repo(f.get("owner")):
                    rep.add("DC7", t, f"`{clean(f.get('owner'))}` is not the owner listed for this table in owners.yaml "
                            f"(`{clean(owners[t])}`)", *where)
            # DC5 also covers the secret-like columns a foreign change adds; the item answers it in its own secret_like
            for c, ln in sorted(added_cols.items()):
                if is_secret_like(c):
                    d = decision(f.get("secret_like"), c)
                    rep.secret_new.append({"table": t, "col": c, "decision": d})
                    if d is None:
                        rep.add("DC5", t, f"new column `{clean(c)}` looks like a secret; list it under the foreign_changes "
                                "item's secret_like as `exclude`, or `keep` with a reason", info["file"], ln)
            continue
        if entry is None:
            rep.add("DC1", t, "is added or changed and has no entry in " + DATA + ". Add scope_class, placement, "
                    "placement_key, meaning, fields and phone_fields.", *where)
            continue
        # DC2
        problems = []
        if entry.get("scope_class") not in SCOPE_CLASSES:
            problems.append(f"scope_class `{clean(entry.get('scope_class'))}` is not one of {', '.join(SCOPE_CLASSES)}")
        if entry.get("placement") not in PLACEMENTS:
            problems.append(f"placement `{clean(entry.get('placement'))}` is not one of {', '.join(PLACEMENTS)}")
        if not str(entry.get("placement_key") or "").strip():
            problems.append("placement_key is empty")
        if not str(entry.get("meaning") or "").strip():
            problems.append("meaning is empty")
        for pr in problems:
            rep.add("DC2", t, "entry: " + pr, *where)
        raw_fields = entry.get("fields") or {}
        fields = {str(k).lower() for k in (raw_fields if isinstance(raw_fields, (dict, list)) else [])}
        phone = entry.get("phone_fields")
        cols_known = {c.lower() for c in new.cols.get(t, {})} | (MONGO_IMPLICIT if t in new.model_names else set())
        has_create = t in new.created
        # DC3: the placement key is `central` or a column of the table after the PR
        key = str(entry.get("placement_key") or "").strip().lower()
        if key and key != "central" and key not in cols_known:
            if has_create:
                rep.add("DC3", t, f"placement_key `{clean(key)}` is neither `central` nor a column of the table", *where)
            else:
                rep.add("DC3", t, f"placement_key `{clean(key)}` could not be verified: no CREATE for this table is in the repo",
                        *where, level="warning")
        # DC4: phone_fields are in fields; fields are columns
        if "phone_fields" in entry and phone is None:  # only the p4 adapter makes this: reaches_phones true, no explicit list
            rep.add("DC4", t, "reaches phones but names no phone_fields; list the fields explicitly (never all of them)", *where)
        for f in (phone or []):
            if str(f).lower() not in fields:
                rep.add("DC4", t, f"phone_fields names `{clean(f)}`, which is not in fields", *where)
        if cols_known or has_create:
            for f in sorted(fields - cols_known):
                rep.add("DC4", t, f"fields names `{clean(f)}`, which the table does not have", *where,
                        level=None if has_create else "warning")
        # DC5 and DC6 on the columns this PR adds
        sl = entry.get("secret_like") or {}
        for c, ln in sorted(added_cols.items()):
            if is_secret_like(c):
                d = decision(sl, c)
                rep.secret_new.append({"table": t, "col": c, "decision": d})
                if d is None:
                    rep.add("DC5", t, f"new column `{clean(c)}` looks like a secret; list it under secret_like as `exclude`, "
                            "or `keep` with a reason", info["file"], ln)
                elif any(str(x).lower() == c.lower() for x in (phone or [])) and d != "keep":
                    rep.add("DC5", t, f"secret-like column `{clean(c)}` is in phone_fields but not marked `keep` with a reason",
                            info["file"], ln)
            if c.lower() not in fields:
                rep.add("DC6", t, f"new column `{clean(c)}` is not in the entry's fields", info["file"], ln)

    # ── DC10: DDL or a collection call outside schema_paths and exclude_paths ───────────────────────────
    known_names = set(entries) | new.created
    for p, lines in sorted(added.items()):
        if p in new.schema_files or matches_any(p, exclude_paths) or not p.endswith(CODE_EXT + (".yaml", ".yml")):
            continue
        text = head_text(p)
        if text is None or p.endswith((".yaml", ".yml")) and "databaseChangeLog" not in text:
            continue
        src, hits = Src(p, text), []
        for ev in sql_events(src, new.res, masked=p.endswith(".sql")):
            if any(ln in lines for ln in range(ev.line, ev.end + 1)):
                hits.append((ev.line, "DDL on " + clean(ev.raw)))
        for m in list(re.finditer(r"createTable\(|\bcreateIndex\b", text)) + list(KNEX_CALL.finditer(text)):
            if src.line(m.start()) in lines:
                hits.append((src.line(m.start()), clean(m.group(0))))
        for m in list(RE_COLL.finditer(text)) + list(RE_MODEL.finditer(text)):
            ln = src.line(m.start())
            if m.re is RE_COLL:
                name, q = m.group(1), new.res.qualify(m.group(1))
            else:
                name, q = m.group("a"), None
                lit = re.match(r"^['\"]([^'\"]+)['\"]$", name.strip())
                if lit:
                    q = new.res.qualify(pluralize(lit.group(1)))
            if ln in lines and (q is None or q not in known_names):
                hits.append((ln, "collection " + clean(name)))
        for ln, what in sorted({ln: what for ln, what in reversed(hits)}.items()):  # one finding per line
            rep.add("DC10", "-", f"{what} outside schema_paths. Add the path to schema_paths in {DATA} (judged under DC8), or "
                    "to exclude_paths with a reason.", p, ln)

    rep.inventory = sorted(t for t in new.created if t not in entries and not excluded_table(t))
    rep.mongo = bool(new.model_names) or any(k in ("mongoose", "mongodriver") for k in new.schema_files.values())
    return rep


# ── output ───────────────────────────────────────────────────────────────────────────────────────────────
def esc(s):
    return clean(s, 200).replace("|", "/").replace("`", "'")


def render(rep, base_ref):
    err, warn = rep.errors, [f for f in rep.findings if f["level"] == "warning"]
    head = "report only: this run never fails" if rep.mode == "report" else ("FAILED" if err else "passed")
    out = [f"## Declaration gate ({rep.mode}): {head}", "",
           f"Base `{esc(base_ref)}`. {len(err)} error(s), {len(warn)} warning(s). Rules: specs offline-first/declaration-ci-check.md section 4.", ""]
    if rep.findings:
        out += ["| Rule | Level | Table | What is wrong |", "|---|---|---|---|"]
        for f in sorted(rep.findings, key=lambda f: (f["level"] != "error", f["rule"], str(f["table"]))):
            at = f" ({esc(f['file'])}:{f['line']})" if f["file"] else ""
            out.append(f"| {f['rule']} | {f['level']} | `{esc(f['table'])}` | {esc(f['msg'])}{at} |")
        out.append("")
    else:
        out += ["No rule fired.", ""]
    out += ["### Tables this PR touches", ""]
    out += [f"- `{esc(t)}`: {', '.join(ops)}" for t, ops in sorted(rep.touched.items())] or ["- none"]
    if rep.unmasked:
        out += ["", f"### Tables {FOUND_UNMASKED}", ""]
        out += [f"- `{esc(t)}`: only the second, unmasked scan saw it (it may sit in a comment, or the comment scan lost its place); "
                "it is judged as touched" for t in sorted(rep.unmasked)]
    if rep.removable:
        out += ["", "### Entries that can be removed", ""]
        out += [f"- `{esc(t)}`: this PR drops the table, so its entry in {DATA} can be removed (a note, not an error)" for t in rep.removable]
    if rep.secret_new:
        out += ["", "### Secret-like new columns", ""]
        out += [f"- `{esc(s['table'])}.{esc(s['col'])}`: {s['decision'] or 'NO DECISION'}" for s in rep.secret_new]
        ex = [s for s in rep.secret_new if s["decision"] == "exclude"]
        if ex:
            out += ["", "Hand these `exclude` columns to OFFLINE-01 for the pipeline's column exclude list: " +
                    ", ".join(f"`{esc(s['table'])}.{esc(s['col'])}`" for s in ex)]
    if rep.foreign:
        out += ["", "### Changes to tables another repo owns (hand to the owner)", ""]
        out += [f"- `{esc(f['table'])}`, owner `{esc(f['owner'])}`" +
                (f", new columns: {', '.join('`' + esc(c) + '`' for c in f['cols'])}" if f["cols"] else "") for f in rep.foreign]
    if rep.excluded:
        out += ["", "### Excluded names (exclude_tables)", ""] + [f"- `{esc(t)}`" for t in rep.excluded]
    out += ["", f"### Inventory (report only, never fails): {len(rep.inventory)} table(s) with no entry", ""]
    if rep.mongo:
        out += ["_Collections registered by other means than mongoose.model(...) are not counted._", ""]
    out += [f"- `{esc(t)}`" for t in rep.inventory[:50]]
    if len(rep.inventory) > 50:
        out.append(f"- ... and {len(rep.inventory) - 50} more")
    out += ["", *[f"_{esc(n)}_" for n in rep.notes]]
    return "\n".join(out) + "\n"


def annotate(rep):
    for f in rep.findings:
        level = "error" if f["level"] == "error" and rep.mode == "block" else "warning"
        loc = f"file={clean(f['file'], 200)},line={f['line']}," if f["file"] else ""
        label = f"{f['rule']} {clean(f['table'])}: {clean(f['msg'], 300)}" + \
            (" (report mode: not blocking)" if f["level"] == "error" and rep.mode != "block" else "")
        print(f"::{level} {loc}title=declaration gate::{label}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base-ref", required=True)
    ap.add_argument("--mode", default="report")
    ap.add_argument("--owners", default=None)
    ap.add_argument("--calling-repo", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    a = ap.parse_args(argv)
    if a.mode not in ("report", "block"):
        print(f"::error title=declaration gate::mode must be `report` or `block`, got `{clean(a.mode)}`")
        return 2
    if yaml is None:
        print(f"::{'error' if a.mode == 'block' else 'warning'} title=declaration gate::PyYAML is not installed on this runner; "
              f"{'blocking (fail closed)' if a.mode == 'block' else 'report mode, not blocking'}")
        return 2 if a.mode == "block" else 0
    try:
        rep = judge(a.repo, a.base_ref, a.mode, a.owners, a.calling_repo)
    except GateError as e:
        print(f"::{'error' if a.mode == 'block' else 'warning'} title=declaration gate::{clean(e, 300)}")
        return 1 if a.mode == "block" else 0
    except Exception as e:  # noqa: BLE001 - a bug in the gate must not fail a report-only run
        print(f"::{'error' if a.mode == 'block' else 'warning'} title=declaration gate::the gate crashed ({type(e).__name__}); "
              f"{'blocking' if a.mode == 'block' else 'report mode, not blocking'}. GIT-01: set mode back to report.")
        return 2 if a.mode == "block" else 0
    text = render(rep, a.base_ref)
    annotate(rep)
    print(text)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if (a.mode == "block" and rep.errors) else 0


if __name__ == "__main__":
    sys.exit(main())
