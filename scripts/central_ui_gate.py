#!/usr/bin/env python3
"""G-CENTRAL-1 and G-CENTRAL-2 (specs flutter-ui/wide-layout-decisions-spec.md §6).

Run from a module repo's root, after `git fetch origin <base>`.
  G-CENTRAL-1: a web_visual manifest row that is new against the base must name its central row
               (`central: <Row>` from client_core CENTRAL_UI.md, used by the screen file), or answer
               `central: "none: no-central-equivalent|flavour-visual"` AND be in central_exceptions.yaml.
  G-CENTRAL-2: no new copy of a central piece: a widget class named in central_ui_names.txt, a route
               path a CENTRAL_UI.md row wires up, or a direct dependency on a package a central piece
               wraps. Findings are computed at head and at the base, and only additions fail, so
               today's copies pass until removed and the list can only shrink.
The three client_core files are read at the `resolved-ref` the module's pubspec.lock pins (else cwb).

Env: BASE_REF (default origin/cwb), GH_TOKEN (read token for pkg_orbit_client_core).
Exit 0 clean, 1 on findings, 2 when an input can't be read (never passes on an unreadable input).
Test: python3 scripts/test_central_ui_gate.py
"""
import os, re, subprocess, sys, urllib.request

MANIFEST = "test/UI/web_visual/manifest.yaml"
CORE_REPO = "purpusgit/pkg_orbit_client_core"
CORE_PKG = "orbit_client_core"
NONE_REASONS = {"no-central-equivalent", "flavour-visual"}
# Packages a central piece wraps: depending on one directly is a renamed copy of the location pickers.
# shortcut: a fixed list, move it into client_core next to central_ui_names.txt when a second wrapper appears.
WRAPPED = {"geocoding": "showLocationPickerSheet", "google_places_flutter": "showPlaceSearchSheet",
           "flutter_google_places": "showPlaceSearchSheet", "google_maps_webservice": "showPlaceSearchSheet",
           "flutter_google_places_sdk": "showPlaceSearchSheet"}

CLASS_RX = re.compile(r"^\s*(?:abstract\s+|final\s+|base\s+)*class\s+([A-Z]\w*)\b", re.M)
ROUTE_RX = re.compile(r"""GetPage\s*\((?:(?!GetPage\s*\().){0,400}?name:\s*['"](/[^'"]*)['"]""", re.S)
INDEX_ROUTE_RX = re.compile(r"`(/[a-z0-9][a-z0-9_\-/:]*)`")
INDEX_ROW_RX = re.compile(r"^\|\s*`([A-Za-z_]\w*)`\s*\|", re.M)


class InputError(Exception):
    pass


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def read_head(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def read_base(base, path):
    return git("show", f"{base}:{path}")


def parse_manifest(text):
    """{screen: {key: value}} from the `screens:` map. shortcut: two indent levels, flat values, the whole
    format web_width_gen reads today; switch to a YAML library if rows ever nest deeper."""
    rows, cur, in_screens = {}, None, False
    for ln in (text or "").splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        t = ln.strip()
        if ind == 0:
            in_screens = t.startswith("screens:")
            cur = None
            continue
        if not in_screens:
            continue
        k, _, v = t.partition(":")
        v = v.strip()
        if ind == 2 and not v:
            cur = rows.setdefault(k.strip(), {})
        elif cur is not None and ind >= 4:
            if v and v[0] not in "\"'":
                v = v.split(" #", 1)[0].strip()
            cur[k.strip()] = v.strip("\"'")
    return rows


def parse_exceptions(text):
    out, cur = [], None
    for ln in (text or "").splitlines():
        t = ln.split(" #", 1)[0].strip() if not ln.lstrip().startswith("#") else ""
        if not t:
            continue
        if t.startswith("- "):
            cur = {}
            out.append(cur)
            t = t[2:]
        if cur is not None and ":" in t:
            k, _, v = t.partition(":")
            cur[k.strip()] = v.strip().strip("\"'")
    return out


def pubspec_name(text):
    m = re.search(r"^name:\s*(\S+)", text or "", re.M)
    return m.group(1) if m else None


def pubspec_deps(text):
    """Direct dependency names under dependencies: / dev_dependencies: / dependency_overrides:."""
    deps, section = set(), False
    for ln in (text or "").splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln.startswith(" "):
            section = ln.split(":")[0] in ("dependencies", "dev_dependencies", "dependency_overrides")
            continue
        m = re.match(r"^  ([a-z_][a-z0-9_]*):", ln)
        if section and m:
            deps.add(m.group(1))
    return deps


def core_ref(lock_text):
    """The git ref pubspec.lock pins for orbit_client_core, or cwb."""
    m = re.search(r"^  %s:\n((?:    .*\n|      .*\n)+)" % CORE_PKG, (lock_text or "") + "\n", re.M)
    if m:
        r = re.search(r"resolved-ref:\s*\"?([0-9a-f]{7,40})", m.group(1))
        if r:
            return r.group(1)
    return "cwb"


def fetch_core(path, ref, token):
    req = urllib.request.Request(f"https://api.github.com/repos/{CORE_REPO}/contents/{path}?ref={ref}",
                                 headers={"Accept": "application/vnd.github.raw",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 - every failure is an unreadable input, reported by name
        raise InputError(f"could not read {CORE_REPO} {path} at {ref}: {e}")


def index_inputs(index, names_text):
    rows = set(INDEX_ROW_RX.findall(index))
    names = {ln.strip() for ln in names_text.splitlines() if ln.strip() and not ln.startswith("#")}
    if not rows or not names:
        raise InputError("CENTRAL_UI.md has no rows or central_ui_names.txt has no names")
    routes = set(INDEX_ROUTE_RX.findall(index))
    return rows, names, routes


def copies(files, pubspec, names, routes):
    """G-CENTRAL-2 findings for one tree: {(kind, name, file)}."""
    found = set()
    for path, text in files.items():
        for c in CLASS_RX.findall(text):
            if c in names:
                found.add(("widget", c, path))
        for r in ROUTE_RX.findall(text):
            if r in routes:
                found.add(("route", r, path))
    for d in pubspec_deps(pubspec):
        if d in WRAPPED:
            found.add(("package", d, "pubspec.yaml"))
    return found


def head_lib():
    out = {}
    for d, _, ns in os.walk("lib"):
        for n in sorted(ns):
            if n.endswith(".dart"):
                p = os.path.join(d, n)
                out[p] = read_head(p) or ""
    return out


def base_lib(base):
    listing = git("ls-tree", "-r", "--name-only", base, "--", "lib")
    if listing is None:
        raise InputError(f"base {base} is not readable (fetch it first)")
    return {p: read_base(base, p) or "" for p in listing.split() if p.endswith(".dart")}


def gate_one(head_rows, base_rows, exceptions, module, index_rows, read_file):
    errs = []
    for screen in sorted(set(head_rows) - set(base_rows)):
        row = head_rows[screen]
        central = row.get("central", "")
        if not central:
            errs.append(f"G-CENTRAL-1 {screen}: a new manifest row needs `central: <CENTRAL_UI.md row>`, or "
                        f"`central: \"none: no-central-equivalent\"` / `\"none: flavour-visual\"` plus a "
                        f"central_exceptions.yaml entry from SCREENS-01")
            continue
        if central.startswith("none"):
            reason = central.partition(":")[2].strip()
            if reason not in NONE_REASONS:
                errs.append(f"G-CENTRAL-1 {screen}: `central: none` needs a reason from {sorted(NONE_REASONS)}, "
                            f"got {reason or 'none'!r}")
            elif not any(e.get("screen") == screen and e.get("module") == module and e.get("reason") == reason
                         for e in exceptions):
                errs.append(f"G-CENTRAL-1 {screen}: `central: none` but client_core central_exceptions.yaml has "
                            f"no entry for screen {screen}, module {module}, reason {reason} (ask SCREENS-01, specs#793)")
            continue
        if central not in index_rows:
            errs.append(f"G-CENTRAL-1 {screen}: `central: {central}` is not a row in CENTRAL_UI.md at the pinned "
                        f"client_core version")
            continue
        src = read_file(row.get("file", "")) or ""
        # shortcut: "imports that row's library" is checked as a client_core import plus the name in the file;
        # resolve the exact library once CENTRAL_UI.md carries a library column.
        if "package:orbit_client_core/" not in src or not re.search(r"\b%s\b" % re.escape(central), src):
            errs.append(f"G-CENTRAL-1 {screen}: names `central: {central}` but {row.get('file') or 'its file'} "
                        f"does not import orbit_client_core and use {central}")
    return errs


def main():
    base = os.environ.get("BASE_REF") or "origin/cwb"
    module = pubspec_name(read_head("pubspec.yaml"))
    if not module:
        print("No pubspec.yaml name: G-CENTRAL does not apply.")
        return 0
    if module == CORE_PKG:
        print("client_core owns the central pieces; its own index check (tool/central_ui_index_check.dart) covers it.")
        return 0
    try:
        ref = core_ref(read_head("pubspec.lock"))
        token = os.environ.get("GH_TOKEN", "")
        index = fetch_core("CENTRAL_UI.md", ref, token)
        rows, names, routes = index_inputs(index, fetch_core("central_ui_names.txt", ref, token))
        exceptions = parse_exceptions(fetch_core("central_exceptions.yaml", ref, token))
        head_rows = parse_manifest(read_head(MANIFEST))
        base_rows = parse_manifest(read_base(base, MANIFEST))
        errs = gate_one(head_rows, base_rows, exceptions, module, rows, read_head)
        added = copies(head_lib(), read_head("pubspec.yaml"), names, routes) - \
            copies(base_lib(base), read_base(base, "pubspec.yaml"), names, routes)
    except InputError as e:
        print(f"::error::G-CENTRAL could not read an input, so it refuses to pass: {e}")
        return 2
    for kind, name, path in sorted(added):
        what = {"widget": f"defines widget {name}, a central piece",
                "route": f"registers route {name}, which a central piece owns",
                "package": f"depends on {name}, which central {WRAPPED.get(name)} wraps"}[kind]
        errs.append(f"G-CENTRAL-2 {path}: {what}. Use the central one from CENTRAL_UI.md, or ask SCREENS-01 to extend it")
    print(f"G-CENTRAL: client_core at {ref}; {len(set(head_rows) - set(base_rows))} new manifest row(s); "
          f"{len(added)} new copy finding(s).")
    for e in errs:
        print(f"::error::{e}")
    if errs:
        return 1
    print("G-CENTRAL passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
