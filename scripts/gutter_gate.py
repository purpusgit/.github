#!/usr/bin/env python3
"""PHONE-EDGE-1 gutter gate (tracker B15, founder 19 Nov 00:38 IST, specs#518 6988736057).

Fails when an added or changed `PageContentWidth(` call in a .dart file does not say what its
gutter is. Allowed: an explicit `padding:` argument (for example `padding: EdgeInsets.zero`),
or the marker comment `// gutter: wrapper` on the call line or within the call (wrapper gutter
on purpose). Usage: gutter_gate.py <base-ref> [<head-ref>]
"""
import re
import subprocess
import sys

MARKER = re.compile(r"//\s*gutter:\s*wrapper")
CALL = re.compile(r"\bPageContentWidth\s*\(")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changed_lines(base, head):
    """{path: set(new line numbers)} for added/changed lines in .dart files."""
    out = git("diff", "--unified=0", "--no-color", f"{base}...{head}", "--", "*.dart")
    files, path = {}, None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = None if line[4:] == "/dev/null" else line[6:]
            if path:
                files.setdefault(path, set())
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            files[path].update(range(start, start + count))
    return files


def call_span(text, start):
    """Return (end_index) of the call whose '(' follows start, by bracket depth."""
    i = text.index("(", start)
    depth = 0
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


def check(base, head="HEAD"):
    bad = []
    for path, lines in changed_lines(base, head).items():
        if not lines:
            continue
        try:
            text = git("show", f"{head}:{path}")
        except subprocess.CalledProcessError:
            continue
        line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(i + 1)

        def line_of(idx):
            lo, hi = 0, len(line_starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if line_starts[mid] <= idx:
                    lo = mid
                else:
                    hi = mid - 1
            return lo + 1

        for m in CALL.finditer(text):
            end = call_span(text, m.start())
            first, last = line_of(m.start()), line_of(end)
            if not lines.intersection(range(first, last + 1)):
                continue  # call not touched by this change
            body = text[m.start():end + 1]
            # top-level `padding:` argument (not inside a nested call)
            depth, top_padding = 0, False
            for tok in re.finditer(r"[()\[\]{}]|\bpadding\s*:\s*(null\b)?", body):
                t = tok.group(0)
                if t in "([{":
                    depth += 1
                elif t in ")]}":
                    depth -= 1
                elif depth == 1 and not tok.group(1):
                    # `padding: null` is the wrapper gutter: it still needs the marker
                    top_padding = True
            line_text = text[line_starts[first - 1]:text.find("\n", m.start()) if "\n" in text[m.start():] else len(text)]
            if top_padding or MARKER.search(body) or MARKER.search(line_text):
                continue
            bad.append(f"{path}:{first}")
    return bad


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: gutter_gate.py <base-ref> [<head-ref>]", file=sys.stderr)
        sys.exit(2)
    found = check(*sys.argv[1:3])
    if found:
        print("PHONE-EDGE-1: these new or changed PageContentWidth( calls do not state their gutter:")
        for f in found:
            print(f"::error file={f.split(':')[0]},line={f.split(':')[1]}::PageContentWidth( needs an explicit padding: (e.g. EdgeInsets.zero) or a '// gutter: wrapper' marker. See coding-rules PHONE-EDGE-1.")
            print("  " + f)
        sys.exit(1)
    print("PHONE-EDGE-1 gutter gate: ok")
