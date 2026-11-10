#!/usr/bin/env python3
"""WEB-SAFE gate: a new use of a mobile-only API in lib/ needs a web answer.

Why (founder, 9 Nov 2026): pkg_orbit_japa's "Share my card" called
MethodChannel('com.orgorbit/share'). Only Android and iOS implement that channel, so on the
web it threw MissingPluginException and the button silently did nothing. The web width gates
check layout only, so nothing caught it.

What it flags in lib/**.dart, only where this change makes it new relative to BASE_REF
(default origin/cwb): an added line, or an existing line whose web answer was removed.
  - MethodChannel / EventChannel / BasicMessageChannel
  - dart:io Platform.*, File(...), Directory(...) in a file that imports dart:io
  - an import of a mobile-only plugin: the KNOWN list below, or a federated plugin that
    pubspec.lock lists with an _android/_ios/_platform_interface package but no _web one

A flagged line passes if ANY of:
  (a) it sits after a kIsWeb check in the same top-level or class-member declaration
      (if (kIsWeb) ... / if (!kIsWeb) ... / kIsWeb ? ... : ...);
  (b) it is in a *_mobile.dart file with a *_web.dart sibling, and some lib/ file wires them
      through a conditional import or export (`if (dart.library.…)`);
  (c) the line, or the line just above it, carries `// web: <what happens on web>`.

Modes:
  (default)   diff gate; exit 1 on any unanswered use, 2 if the diff cannot be computed.
  --backlog   every unanswered use in lib/ today, as a report; always exits 0.
  --demo      self-test on a throwaway git repo: one passing and one failing sample per rule.
"""
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

CHANNEL = re.compile(r"\b(MethodChannel|EventChannel|BasicMessageChannel)\b")
DART_IO = re.compile(r"""^\s*import\s+['"]dart:io['"]""", re.M)
IO_USE = re.compile(r"\bPlatform\.[a-zA-Z]+|(?<![\w.])(File|Directory)\s*(\(|\.from)")
PKG_IMPORT = re.compile(r"""^\s*(?:import|export)\s+['"]package:([a-z0-9_]+)/""")
KIS_WEB = re.compile(r"\bkIsWeb\b")
WEB_NOTE = re.compile(r"//\s*web:\s*\S")
COND_IMPORT = re.compile(r"""\bif\s*\(\s*dart\.library\.""")
# Plugins with no web implementation that we already use (founder's list, 9 Nov 2026, plus the
# ones in pkg_orbit_japa's pubspec.yaml). Named here because library packages gitignore
# pubspec.lock, so the federated-plugin check below finds nothing there.
KNOWN_MOBILE = {"flutter_background_service", "flutter_contacts", "permission_handler",
                "vosk", "vosk_flutter", "record", "local_auth", "flutter_local_notifications",
                "live_activities", "simple_pip_mode", "in_app_update", "in_app_purchase_storekit",
                "mic_stream", "video_thumbnail"}

FIXES = ("Give it a web answer, one of:\n"
         "  (a) guard it: if (kIsWeb) { <web path> } else { <this call> }\n"
         "  (b) move it into a *_mobile.dart file with a *_web.dart sibling and pick one with a\n"
         "      conditional import: import 'x_web.dart' if (dart.library.io) 'x_mobile.dart';\n"
         "  (c) say why the web is safe or never gets here: <code> // web: <what happens on web>")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def lock_packages(root):
    path = os.path.join(root, "pubspec.lock")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return set(re.findall(r"^  ([a-z0-9_]+):\s*$", fh.read(), re.M))


def mobile_only_plugins(root):
    pkgs = lock_packages(root)
    # shortcut: federated-plugin naming only; a plugin that is not federated and has no web
    # code is caught only via KNOWN_MOBILE. Upgrade to reading pubspec flutter.plugin.platforms
    # from the pub cache if that misses real cases.
    fed = {p for p in pkgs for s in ("_android", "_ios", "_platform_interface")
           if p + s in pkgs and p + "_web" not in pkgs}
    return KNOWN_MOBILE | fed


def conditional_wired(root, files):
    """Basenames named on a conditional import/export line anywhere in lib/."""
    wired = set()
    for path in files:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # a conditional import may wrap lines; join each import/export statement first
        for stmt in re.findall(r"\b(?:import|export)\b[^;]*;", text):
            if COND_IMPORT.search(stmt):
                wired.update(os.path.basename(m) for m in re.findall(r"""['"]([^'"]+\.dart)['"]""", stmt))
    return wired


TYPE_DECL = re.compile(r"\b(class|mixin|extension|enum)\b")


def member_start(lines, idx):
    """Index of the line that opens the top-level or class-member declaration holding idx."""
    depth, before = 0, []
    for s in lines[: idx + 1]:
        before.append(depth)
        code = s.split("//")[0]
        depth = max(0, depth + code.count("{") - code.count("}"))
    tops = [i for i in range(idx + 1) if before[i] == 0]
    top = tops[-1] if tops else 0
    if before[idx] >= 2 and TYPE_DECL.search(lines[top]):
        members = [i for i in range(top + 1, idx + 1) if before[i] == 1]
        return members[-1] if members else top
    return top


def answered(path, lines, idx, root, wired):
    line = lines[idx]
    if WEB_NOTE.search(line) or (idx > 0 and WEB_NOTE.search(lines[idx - 1])
                                 and lines[idx - 1].lstrip().startswith("//")):
        return True
    base = os.path.basename(path)
    if base.endswith("_mobile.dart"):
        web = base[: -len("_mobile.dart")] + "_web.dart"
        if os.path.exists(os.path.join(root, os.path.dirname(path), web)) and \
                (base in wired or web in wired):
            return True
    start = member_start(lines, idx)
    return any(KIS_WEB.search(lines[i].split("//")[0]) for i in range(start, idx + 1))


def hits_in_line(line, has_io, plugins):
    code = line.split("//")[0]
    if not code.strip():
        return []
    out = [m.group(1) for m in CHANNEL.finditer(code)]
    if has_io:
        out += [m.group(0).rstrip("(.").strip() for m in IO_USE.finditer(code)]
    m = PKG_IMPORT.match(code)
    if m and m.group(1) in plugins:
        out.append("import of " + m.group(1) + " (no web implementation)")
    return out


def lib_files(root):
    files = []
    for d, _, names in os.walk(os.path.join(root, "lib")):
        files += [os.path.relpath(os.path.join(d, n), root) for n in sorted(names) if n.endswith(".dart")]
    return files


def scan_text(path, text, root, wired, plugins):
    """[(line number, what, stripped line)] for every unanswered use in one file's text."""
    lines, has_io, out = text.splitlines(), bool(DART_IO.search(text)), []
    for i, line in enumerate(lines):
        for what in dict.fromkeys(hits_in_line(line, has_io, plugins)):  # once per line
            if not answered(path, lines, i, root, wired):
                out.append((i + 1, what, line.strip()))
    return out


def read(root, path):
    with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def scan(root, base=None):
    """Unanswered uses. With base: only those this change adds (a new line, or an old line
    whose kIsWeb guard or // web: note was removed), compared per file by line content."""
    files = lib_files(root)
    plugins, wired = mobile_only_plugins(root), conditional_wired(root, files)
    if base is not None:
        changed = set(git("diff", "--name-only", base + "...HEAD", "--", "lib", cwd=root).split())
        files = [f for f in files if f in changed]
    found = []
    for path in files:
        now = scan_text(path, read(root, path), root, wired, plugins)
        if base is not None:
            try:
                before = git("show", base + ":" + path, cwd=root)
            except subprocess.CalledProcessError:
                before = ""  # a new file
            seen = Counter((w, l) for _, w, l in scan_text(path, before, root, wired, plugins))
            kept = []
            for n, w, l in now:
                if seen[(w, l)]:
                    seen[(w, l)] -= 1  # already unanswered on base: backlog, not this PR
                else:
                    kept.append((n, w, l))
            now = kept
        found += [(path, n, w) for n, w, _ in now]
    return found


def gate(root, base):
    try:
        git("rev-parse", "--verify", "-q", base + "^{commit}", cwd=root)
        found = scan(root, base)
    except subprocess.CalledProcessError as e:
        print("::error::WEB-SAFE could not diff against " + base + ": " + (e.stderr or "").strip()
              + ". This is a gate failure, not a clean tree.")
        return 2
    print("WEB-SAFE: checked lib/ files changed since " + base + ".")
    for path, n, what in found:
        print("::error file=" + path + ",line=" + str(n) + "::" + what
              + " runs on mobile only and has no web answer.")
        print(path + ":" + str(n) + ": " + what)
    if found:
        print("\n" + str(len(found)) + " mobile-only use(s) with no web answer.\n" + FIXES)
        return 1
    print("WEB-SAFE passed: every new mobile-only use has a web answer.")
    return 0


def backlog(root):
    found = scan(root)
    print("# WEB-SAFE backlog: " + str(len(found)) + " unanswered mobile-only use(s) in lib/\n")
    for path, n, what in found:
        print("- `" + path + ":" + str(n) + "` " + what)
    return 0


# ── self-test ─────────────────────────────────────────────────────────────────
DEMO_BASE = {
    "pubspec.lock": "packages:\n  foo_plugin:\n    x\n  foo_plugin_android:\n    x\n"
                    "  url_launcher:\n    x\n  url_launcher_android:\n    x\n  url_launcher_web:\n    x\n",
    # pre-existing unguarded use: diff mode must NOT flag it, backlog mode must
    "lib/old.dart": "import 'package:flutter/services.dart';\n"
                    "Future<void> old() => const MethodChannel('x').invokeMethod('y');\n",
    # guarded on base; the failing sample strips the guard but keeps the channel line as is
    "lib/guarded.dart": "import 'package:flutter/foundation.dart';\nimport 'package:flutter/services.dart';\n"
                        "Future<void> go() async {\n  if (kIsWeb) return;\n"
                        "  await const MethodChannel('g').invokeMethod('go');\n}\n",
}
DEMO_PASS = {
    "lib/guard.dart": "import 'package:flutter/foundation.dart';\nimport 'package:flutter/services.dart';\n"
                      "Future<void> share(String t) async {\n  if (kIsWeb) {\n    return;\n  }\n"
                      "  await const MethodChannel('s').invokeMethod('share', t);\n}\n",
    "lib/note.dart": "import 'package:flutter/services.dart';\n"
                     "const ch = EventChannel('e'); // web: never listened to; the web build hides this screen\n",
    "lib/store.dart": "export 'store_web.dart' if (dart.library.io) 'store_mobile.dart';\n",
    "lib/store_web.dart": "String home() => '/';\n",
    "lib/store_mobile.dart": "import 'dart:io';\nString home() => Directory.current.path;\n",
    "lib/links.dart": "import 'package:url_launcher/url_launcher.dart';\n",
    "lib/member.dart": "import 'package:flutter/foundation.dart';\nimport 'package:flutter/services.dart';\n"
                       "class S {\n  Future<void> go() async {\n    if (kIsWeb) {\n      return;\n    }\n"
                       "    try {\n      await const MethodChannel('m').invokeMethod('go');\n    } catch (_) {}\n  }\n}\n",
}
DEMO_FAIL = {
    "lib/share_card_sheet.dart": "import 'package:flutter/services.dart';\n"
                                 "Future<void> shareTextNatively(String text) async {\n"
                                 "  await const MethodChannel('com.orgorbit/share').invokeMethod('shareProfile', text);\n}\n",
    "lib/io.dart": "import 'dart:io';\nbool get android => Platform.isAndroid;\n",
    "lib/perm.dart": "import 'package:permission_handler/permission_handler.dart';\n",
    "lib/fed.dart": "import 'package:foo_plugin/foo_plugin.dart';\n",
    "lib/lonely_mobile.dart": "import 'dart:io';\nvar f = File('a');\n",
    "lib/guarded.dart": "import 'package:flutter/services.dart';\n"
                        "Future<void> go() async {\n"
                        "  await const MethodChannel('g').invokeMethod('go');\n}\n",
}


def _write(root, files):
    for rel, body in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            fh.write(body)


def _repo(head):
    root = tempfile.mkdtemp(prefix="websafe-")
    git("init", "-q", "-b", "base", cwd=root)
    _write(root, DEMO_BASE)
    git("add", "-A", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=root)
    git("checkout", "-qb", "head", cwd=root)
    _write(root, head)
    git("add", "-A", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head", cwd=root)
    return root


def demo():
    bad = 0

    def check(cond, msg):
        nonlocal bad
        print(("ok   " if cond else "FAIL ") + msg)
        bad += 0 if cond else 1

    root = _repo(DEMO_PASS)
    found = scan(root, "base")
    check(found == [], "passing sample passes (kIsWeb guard, // web: note, _mobile/_web pair, "
                       "plugin with a web package); got " + repr(found))
    check(any(p == "lib/old.dart" for p, _, _ in scan(root)), "backlog lists pre-existing lib/old.dart")
    check(gate(root, "no-such-ref") == 2, "an unresolvable base fails closed")

    root = _repo(DEMO_FAIL)
    got = {(p, w) for p, _, w in scan(root, "base")}
    want = {("lib/share_card_sheet.dart", "MethodChannel"), ("lib/io.dart", "Platform.isAndroid"),
            ("lib/perm.dart", "import of permission_handler (no web implementation)"),
            ("lib/fed.dart", "import of foo_plugin (no web implementation)"),
            ("lib/lonely_mobile.dart", "File"), ("lib/guarded.dart", "MethodChannel")}
    check(got == want, "failing sample flags exactly the 6 unanswered uses (incl. a removed guard); got " + repr(sorted(got)))
    check(not any(p == "lib/old.dart" for p, _ in got), "diff mode ignores pre-existing code")
    check(gate(root, "base") == 1, "gate exits 1 on the failing sample")
    print("WEB-SAFE --demo: " + ("all checks passed" if not bad else str(bad) + " check(s) FAILED"))
    return 1 if bad else 0


if __name__ == "__main__":
    if "--demo" in sys.argv:
        sys.exit(demo())
    if "--backlog" in sys.argv:
        sys.exit(backlog("."))
    sys.exit(gate(".", os.environ.get("BASE_REF", "origin/cwb")))
