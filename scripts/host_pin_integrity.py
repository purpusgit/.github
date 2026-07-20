#!/usr/bin/env python3
"""host_pin_integrity.py — host-app pubspec pin integrity gate + auto-bump helper.

Canonical home: purpusgit/.github :: scripts/host_pin_integrity.py
Consumed by:    .github/workflows/reusable-host-pin-integrity.yml   (mode: check)
                .github/workflows/reusable-host-pin-autobump.yml    (mode: bump)

WHY THIS EXISTS
---------------
main_org_orbit (default branch `cwb`) pins each internal `pkg_*` package to a
hardcoded commit SHA under `dependency_overrides` in pubspec.yaml. Two real
failure classes have shipped:

  (a) STRIPPED KEY / ORPHAN COMMENT — a `pub upgrade` (or a bad merge) removes
      the `git:` override key but leaves the explanatory comment behind. The
      package silently re-resolves to its branch tip and a broken cwb HEAD
      ships to every flavour. (Confirmed 2026-07-18 for new_social.)
  (b) SSH URL — an internal override uses `git@github.com:` instead of
      `https://github.com/`. CI must rewrite SSH->HTTPS with a token
      (`insteadOf`) or `flutter pub get` cannot auth; HTTPS is the safer floor.

MODE: check  (the GATE — reusable-host-pin-integrity.yml)
  FAILS (exit 1) on:
    (a) a SHA-pin comment names an internal package that has NO live `git:`
        override key under dependency_overrides (orphan / stripped-key), OR
    (b) any internal `git:` override URL is SSH (severity configurable).
  WARNS (annotation, never fails) when an override `ref` SHA is behind the
  package's live branch HEAD (needs a read token; skipped without one).

MODE: bump   (reusable-host-pin-autobump.yml)
  Rewrites the matching override `ref:` to a new 40-char SHA in place.

Standard library only (urllib) — no pip install needed on the runner.

Rule 39 / Rule 70A overlap check: grepped Rule 13 script index + `.github`
scripts/ (only taxo_lint.py present) — no existing script parses host pubspec
pin integrity or bumps override refs. New script justified; the two modes share
ONE overrides-block parser (no duplicate parser — Rule 70A / Rule 39).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

# -- internal-package recognition -------------------------------------------
# An "internal package" is one we pin by git SHA. A comment token counts as an
# internal reference only if it is `pkg_*`, `orbit_*`, or a known short-name.
# (Data kept tiny + overridable via --internal-name; not a hardcoded data list
#  in the Rule 70B sense.)
INTERNAL_ALLOWLIST = {"new_social", "inapp_chat", "client_core", "japa", "binder"}

_INTERNAL_TOKEN = re.compile(r"[A-Za-z0-9_]+")
# comment language that marks a line as describing a SHA pin / a stripped key
_PIN_KW = re.compile(r"sha[-\s]?pin|pinned|pin to|pin the|stripped|override key", re.I)
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_SUPPRESS = re.compile(r"host-pin:\s*ignore", re.I)


def _norm(name: str) -> str:
    """Collapse repo/key/alias spellings to one comparable stem.

    pkg_orbit_japa / orbit_japa / japa            -> japa
    pkg_orbit_client_core / orbit_client_core     -> client_core
    pkg_orbit_binder / binder                     -> binder
    pkg_new_social / new_social                   -> new_social
    pkg_inapp_chat / inapp_chat                   -> inapp_chat
    """
    n = name.lower()
    if n.startswith("pkg_"):
        n = n[4:]
    # strip the `orbit_` namespace prefix but keep the rest
    n = n.replace("orbit_", "")
    return n


def _internal_tokens(text: str, extra_names: set) -> set:
    toks = set()
    allow = INTERNAL_ALLOWLIST | extra_names
    for m in _INTERNAL_TOKEN.finditer(text):
        t = m.group(0).lower()
        if re.fullmatch(r"pkg_[a-z0-9_]+", t) or re.fullmatch(r"orbit_[a-z0-9_]+", t) or t in allow:
            toks.add(t)
    return toks


# -- pubspec overrides-block parser (shared by check + bump) -----------------
class Override:
    __slots__ = ("key", "start_line", "is_git", "url", "ref", "ref_line", "repo")

    def __init__(self, key, start_line):
        self.key = key
        self.start_line = start_line       # 1-based line of `  key:`
        self.is_git = False
        self.url = None
        self.ref = None
        self.ref_line = None               # 1-based line of the `ref:` value
        self.repo = None                   # repo basename from url (no .git)


def _repo_from_url(url):
    # git@github.com:purpusgit/pkg_x.git   |  https://github.com/purpusgit/pkg_x.git
    m = re.search(r"[:/]purpusgit/([A-Za-z0-9._-]+?)(?:\.git)?$", url.strip())
    return m.group(1) if m else None


def parse_pubspec(lines):
    """Return (overrides:list[Override], comment_lines:list[(lineno,text)]).

    Only the `dependency_overrides:` block is inspected. comment_lines holds every
    `#` line inside that block (1-based line number, raw text).
    """
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^dependency_overrides:\s*$", ln):
            start = i
            break
    if start is None:
        return [], []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        # a new top-level key (col-0, non-comment, non-blank) ends the block
        if re.match(r"^[^#\s]", lines[i]):
            end = i
            break

    overrides = []
    comments = []
    cur = None
    for i in range(start + 1, end):
        raw = lines[i]
        lineno = i + 1
        stripped = raw.strip()
        if stripped.startswith("#"):
            comments.append((lineno, raw))
            continue
        if not stripped:
            continue
        m_key = re.match(r"^  ([A-Za-z0-9_]+):\s*(.*)$", raw)
        if m_key:
            cur = Override(m_key.group(1), lineno)
            overrides.append(cur)
            continue
        if cur is not None:
            if re.match(r"^\s+git:\s*$", raw):
                cur.is_git = True
            m_url = re.match(r"^\s+url:\s*(.+?)\s*$", raw)
            if m_url:
                cur.url = m_url.group(1).strip().strip('"').strip("'")
                cur.repo = _repo_from_url(cur.url)
                cur.is_git = True
            m_ref = re.match(r"^\s+ref:\s*(.+?)\s*$", raw)
            if m_ref:
                cur.ref = m_ref.group(1).strip().strip('"').strip("'")
                cur.ref_line = lineno
    return overrides, comments


def _comment_blocks(comments):
    """Group (lineno, raw) comment tuples into contiguous blocks.

    Two comment lines belong to the same block when their line numbers are
    consecutive. Returns a list of blocks, each a list of (lineno, raw).
    """
    blocks = []
    cur = []
    prev = None
    for lineno, raw in comments:
        if prev is not None and lineno != prev + 1:
            blocks.append(cur)
            cur = []
        cur.append((lineno, raw))
        prev = lineno
    if cur:
        blocks.append(cur)
    return blocks


# -- GitHub API (stdlib) -----------------------------------------------------
def _gh_get(url, token):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "host-pin-integrity"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def live_head(repo, branch, token):
    try:
        d = _gh_get("https://api.github.com/repos/purpusgit/%s/commits/%s" % (repo, branch), token)
        return d.get("sha")
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return None


def behind_by(repo, ref, head, token):
    try:
        d = _gh_get("https://api.github.com/repos/purpusgit/%s/compare/%s...%s" % (repo, ref, head), token)
        # ahead_by = commits head is ahead of ref  ==  how far ref is behind
        return d.get("ahead_by")
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return None


# -- annotations -------------------------------------------------------------
def err(msg, path, line=None):
    loc = "file=%s" % path + (",line=%d" % line if line else "")
    print("::error %s::%s" % (loc, msg))


def warn(msg, path, line=None):
    loc = "file=%s" % path + (",line=%d" % line if line else "")
    print("::warning %s::%s" % (loc, msg))


# -- mode: check -------------------------------------------------------------
def run_check(args):
    path = args.pubspec
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    extra = set(n.lower() for n in (args.internal_name or []))
    overrides, comments = parse_pubspec(lines)

    git_overrides = [o for o in overrides if o.is_git]
    known_present = set()
    for o in overrides:
        known_present.add(_norm(o.key))
        if o.repo:
            known_present.add(_norm(o.repo))

    failures = 0

    # (a) orphan comment / stripped key.
    # Group contiguous `#` lines into blocks. A pin keyword and the package token
    # often sit on DIFFERENT lines of the same block (e.g. the token on the header
    # line, "stripped" three lines down), so detection is block-scoped, not
    # per-line. A block that (i) mentions pin language anywhere, (ii) names an
    # internal package whose normalized stem is NOT a live override key, and
    # (iii) is not suppressed -> orphan.
    for block in _comment_blocks(comments):
        block_text = "".join(t for _, t in block)
        if _SUPPRESS.search(block_text):
            continue
        if not _PIN_KW.search(block_text):
            continue
        reported = set()
        for lineno, raw in block:
            for tok in _internal_tokens(raw, extra):
                stem = _norm(tok)
                if stem in known_present or stem in reported:
                    continue
                reported.add(stem)
                err(
                    "Orphan SHA-pin comment: references internal package '%s' but no "
                    "live git: override key exists under dependency_overrides. A stripped "
                    "override re-resolves the package to its branch tip (2026-07-18 new_social "
                    "incident). Restore the git: override or delete the stale comment. "
                    "(suppress with '# host-pin: ignore')" % tok,
                    path, lineno,
                )
                failures += 1

    # (b) SSH URL on internal git overrides
    ssh_is_error = args.ssh_severity == "error"
    for o in git_overrides:
        if o.url and o.url.startswith("git@github.com:"):
            msg = (
                "Internal git override '%s' uses SSH URL '%s'. Use "
                "https://github.com/purpusgit/%s.git so CI can auth via a token "
                "rewrite (insteadOf). SSH deps fail pub get without an SSH key on the runner."
                % (o.key, o.url, o.repo or "<repo>")
            )
            if ssh_is_error:
                err(msg, path, o.start_line)
                failures += 1
            else:
                warn(msg, path, o.start_line)

    # (c) behind-HEAD warn (advisory)
    token = os.environ.get("GH_READ_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        for o in git_overrides:
            if not (o.ref and _SHA40.match(o.ref) and o.repo):
                continue
            head = live_head(o.repo, args.head_branch, token)
            if not head or head == o.ref:
                continue
            n = behind_by(o.repo, o.ref, head, token)
            if n and n > 0:
                warn(
                    "'%s' pinned at %s is %d commit(s) behind %s@%s HEAD %s. "
                    "Bump intentionally when rolling forward."
                    % (o.key, o.ref[:8], n, o.repo, args.head_branch, head[:8]),
                    path, o.ref_line,
                )
            elif n is None:
                warn(
                    "'%s' pin %s differs from %s@%s HEAD %s (ancestry undetermined)."
                    % (o.key, o.ref[:8], o.repo, args.head_branch, head[:8]),
                    path, o.ref_line,
                )
    else:
        print("::notice::No read token (GH_READ_TOKEN/GITHUB_TOKEN) - skipping behind-HEAD check.")

    if failures:
        print("\nhost-pin integrity gate FAILED: %d error(s)." % failures)
        return 1
    print("host-pin integrity gate passed.")
    return 0


# -- mode: bump --------------------------------------------------------------
def run_bump(args):
    path = args.pubspec
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if not _SHA40.match(args.sha):
        print("::error::--sha '%s' is not a 40-char hex SHA." % args.sha)
        return 1
    overrides, _ = parse_pubspec(lines)
    want = _norm(args.package)
    match = None
    for o in overrides:
        if not o.is_git:
            continue
        if _norm(o.key) == want or (o.repo and _norm(o.repo) == want):
            match = o
            break
    if match is None:
        print("::error::No git override under dependency_overrides matches package "
              "'%s'. Nothing to bump." % args.package)
        return 1
    if match.ref_line is None:
        print("::error::Override '%s' has no ref: line to rewrite." % match.key)
        return 1
    old = match.ref
    if old == args.sha:
        print("::notice::'%s' already at %s - no change." % (match.key, args.sha[:8]))
        return 0
    idx = match.ref_line - 1
    lines[idx] = re.sub(r"(ref:\s*)(['\"]?)[^'\"\s]+(['\"]?)",
                        lambda m: "%s%s%s%s" % (m.group(1), m.group(2), args.sha, m.group(3)),
                        lines[idx])
    print("%s: ref %s -> %s" % (match.key, old, args.sha))
    if args.write:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print("::notice::wrote %s" % path)
    else:
        print("(dry run - pass --write to persist)")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="host-app pubspec pin integrity gate + auto-bump")
    sub = p.add_subparsers(dest="mode", required=True)

    c = sub.add_parser("check", help="run the integrity gate")
    c.add_argument("pubspec", help="path to the host pubspec.yaml")
    c.add_argument("--ssh-severity", choices=["error", "warn"], default="error",
                   help="how to treat SSH git URLs (default: error)")
    c.add_argument("--head-branch", default="cwb",
                   help="package branch to compare pins against (default: cwb)")
    c.add_argument("--internal-name", action="append",
                   help="extra internal package short-name (repeatable)")
    c.set_defaults(func=run_check)

    b = sub.add_parser("bump", help="rewrite an override ref to a new SHA")
    b.add_argument("--pubspec", required=True, help="path to the host pubspec.yaml")
    b.add_argument("--package", required=True, help="package name or override key to bump")
    b.add_argument("--sha", required=True, help="new 40-char commit SHA")
    b.add_argument("--write", action="store_true", help="persist the change (default: dry run)")
    b.set_defaults(func=run_bump)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
