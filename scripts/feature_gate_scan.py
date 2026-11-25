#!/usr/bin/env python3
"""FEATURE-GATE-1 scanner (specs ci-git/spec-attest-gate-and-morning-suite.md, Part C, rev 0.3). IAC-00, 26 Nov 2026.

Finds code that hides or switches off a built feature. Founder, 26 Nov: "people put the gates and hide things and
never remember to tell me to unlock those gates."

  feature_gate_scan.py full <repo-root> [--repo NAME]     every hit in tracked lib/** and src/** files (the inventory)
  feature_gate_scan.py diff <repo-root> <base-ref|A..B>   only hits on lines the branch ADDS (base...HEAD, or a range)

Prints JSON lines: {"id","rule","file","line","text"}; id = sha1(repo, rule, file, symbol), stable across line moves.
Report-only for now; the blocking mode reads approvals from the founder's review page (never a GitHub identity).
Exit 0 always in `full`; `diff` exits 3 when it finds hits.
"""
import hashlib, json, os, re, subprocess, sys

SRC = re.compile(r'^(lib|src)/')
TEST = re.compile(r'(^|/)(test|integration_test|__tests__)/|_test\.dart$|\.(check|test|spec)\.ts$')
DART, TS = re.compile(r'\.dart$'), re.compile(r'\.(ts|js)$')
FLAGWORD = r'(enable|enabled|show|visible|shown|feature|flag)'
RULES = [
    # Dart
    ('D1', DART, re.compile(r'\b(static\s+)?(const|final)\s+bool\s+(\w*(Enabled|Visible|Shown|Enable|Show)\w*)\s*=\s*false\b|\bconst\s+bool\s+(k\w+)\s*=\s*false\b')),
    ('D2', DART, re.compile(r'bool\.fromEnvironment\(\s*[\'"](\w+)[\'"](?![^)]*defaultValue:\s*true)')),
    ('D3', DART, re.compile(r'getBool\(\s*[\'"](\w*' + FLAGWORD + r'\w*)[\'"]\s*\)\s*\?\?\s*false', re.I)),
    ('D4', DART, re.compile(r'\bif\s*\(\s*false\s*\)|&&\s*false\b|Visibility\(\s*visible:\s*false|Offstage\(\s*offstage:\s*true|IgnorePointer\(\s*ignoring:\s*true')),
    ('D5', DART, re.compile(r'(if\s*\(\s*kDebugMode\s*\)|kDebugMode\s*\?)\s*(const\s+)?([A-Z]\w*(Entry|Tile|Button|Screen|Page|Tab|Card|Item|Route)|GoRoute)\(')),
    ('D6', DART, re.compile(r'^\s*//\s*(GoRoute|NavigationDestination|BottomNavigationBarItem|[A-Z]\w*(Tile|Tab|Card|Item|Screen|Page))\(')),
    ('D7', DART, re.compile(r'\bcomingSoon\b|ComingSoon|[Cc]oming soon')),
    # TypeScript / JS
    ('T1', TS, re.compile(r'(\b(?:process\.)?env\.(\w+)\s*===\s*[\'"](true|1)[\'"]|!!\s*process\.env\.(\w+)|Boolean\(\s*process\.env\.(\w+)\s*\))')),
    ('T2', TS, re.compile(r'(\?\?|\|\|)\s*[\'"]false[\'"]')),
    ('T3', TS, re.compile(r'status\((404|501|503)\)[^\n]{0,200}(disabled|not enabled|coming soon)', re.I)),
    ('T4', TS, re.compile(r'^\s*//\s*(router|app|routerApi)\.(get|post|put|patch|delete|use)\(')),
    ('T7', TS, re.compile(r'[Cc]oming soon')),
]
T1_NAME = re.compile(r'(_ENABLED|_ENABLE|_FLAG|_ON)$|^(ENABLE|FEATURE|SHOW)_')
LOG_DEBUG = re.compile(r'kDebugMode\s*\)\s*(debugPrint|print|log|developer\.log)')


def tracked(root):
    out = subprocess.run(['git', 'ls-files'], cwd=root, capture_output=True, text=True).stdout.split('\n')
    return [f for f in out if f and SRC.match(f) and not TEST.search(f) and (DART.search(f) or TS.search(f))]


def symbol(rule, m, line):
    for g in m.groups()[::-1] if m.groups() else []:
        if g and re.fullmatch(r'\w{3,}', g) and g not in ('true', 'false', 'const', 'final', 'static'):
            return g
    return re.sub(r'\s+', ' ', line.strip())[:80]


def scan_line(repo, f, n, line):
    hits = []
    for rule, ext, rx in RULES:
        if not ext.search(f):
            continue
        m = rx.search(line)
        if not m:
            continue
        if rule == 'T1':
            name = next((g for g in (m.group(2), m.group(4), m.group(5)) if g), '')
            if not T1_NAME.search(name):
                continue
        if rule == 'D5' and LOG_DEBUG.search(line):
            continue
        sym = symbol(rule, m, line)
        hid = hashlib.sha1(f'{repo}|{rule}|{f}|{sym}'.encode()).hexdigest()[:16]
        hits.append({'id': hid, 'rule': rule, 'file': f, 'line': n, 'symbol': sym, 'text': line.strip()[:200]})
    return hits


def full(root, repo):
    for f in tracked(root):
        try:
            lines = open(os.path.join(root, f), encoding='utf-8', errors='replace').read().split('\n')
        except OSError:
            continue
        for i, l in enumerate(lines, 1):
            for h in scan_line(repo, f, i, l):
                print(json.dumps(h))


GH_MODE = '--gh' in sys.argv
GH = ('::warning file=%s,line=%s,title=FEATURE-GATE-1 (%s, report-only)::This adds a switched-off or hidden feature: %s. '
      'The founder approves exceptions on the IAC review page.')


def diff(root, base, repo):
    rng = base if '..' in base else f'{base}...HEAD'   # CI passes HEAD^1..HEAD (the merge commit's own change)
    out = subprocess.run(['git', 'diff', '-U0', rng, '--', 'lib', 'src'], cwd=root, capture_output=True, text=True).stdout
    f, n, found = None, 0, 0
    for l in out.split('\n'):
        if l.startswith('+++ '):
            f = l[6:] if l.startswith('+++ b/') else None
        elif l.startswith('@@'):
            n = int(re.search(r'\+(\d+)', l).group(1)) - 1
        elif l.startswith('+') and f and not TEST.search(f):
            n += 1
            for h in scan_line(repo, f, n, l[1:]):
                print(GH % (h['file'], h['line'], h['rule'], h['text'][:150]) if GH_MODE else json.dumps(h)); found += 1
    return found


if __name__ == '__main__':
    a = sys.argv[1:]
    repo = a[a.index('--repo') + 1] if '--repo' in a else os.path.basename(os.path.abspath(a[1]))
    if a[0] == 'full':
        full(a[1], repo)
    elif a[0] == 'diff':
        sys.exit(3 if diff(a[1], a[2], repo) else 0)
    else:
        sys.exit(__doc__)
