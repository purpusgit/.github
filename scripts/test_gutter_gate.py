#!/usr/bin/env python3
"""Self-test for scripts/gutter_gate.py (PHONE-EDGE-1, tracker B15). Temp git repos, no network.

Each case commits a base, then a head with one .dart change, runs the real gate, and asserts the exit code
(and, for FAIL cases, the diagnostic), so a gate that never reds is caught.
"""
import os, subprocess, sys, tempfile

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gutter_gate.py")

CASES = [
    ("no padding, no marker", 'Widget a() => PageContentWidth(\n  child: Text("x"),\n);\n', 1),
    ("padding only in a nested widget", 'Widget a() => PageContentWidth(\n  child: Padding(padding: EdgeInsets.all(8), child: X()),\n);\n', 1),
    ("padding: null without marker", 'Widget a() => PageContentWidth(padding: null, child: X());\n', 1),
    ("explicit EdgeInsets.zero", 'Widget a() => PageContentWidth(padding: EdgeInsets.zero, child: X());\n', 0),
    ("wrapper marker", 'Widget a() => PageContentWidth( // gutter: wrapper\n  child: X(),\n);\n', 0),
    ("non-dart file ignored", None, 0),
]
BASE_UNTOUCHED = 'Widget old() => PageContentWidth(child: Old());\n'


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def case(name, head_text, want):
    with tempfile.TemporaryDirectory() as d:
        g = lambda *a: run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], d)
        g("init", "-q")
        open(os.path.join(d, "old.dart"), "w").write(BASE_UNTOUCHED)  # untouched violation must be skipped
        g("add", "-A"); g("commit", "-qm", "base")
        base = run(["git", "rev-parse", "HEAD"], d).stdout.strip()
        if head_text is None:
            open(os.path.join(d, "notes.txt"), "w").write("PageContentWidth(\n")
        else:
            open(os.path.join(d, "new.dart"), "w").write(head_text)
        g("add", "-A"); g("commit", "-qm", "head")
        r = run([sys.executable, "-I", GATE, base, "HEAD"], d)
        got = 0 if r.returncode == 0 else 1
        ok = got == want and (want == 0 or "PHONE-EDGE-1" in r.stdout)
        print(("  ✓ " if ok else "  ✗ ") + f"{name}: exit {r.returncode}")
        return ok


if __name__ == "__main__":
    results = [case(*c) for c in CASES]
    if not all(results):
        print("gutter gate self-test: RED")
        sys.exit(1)
    print(f"gutter gate self-test: {len(results)} cases GREEN")
