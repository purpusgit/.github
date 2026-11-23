#!/usr/bin/env python3
"""GOOGLE-CLIENT-1 live gate: every Google OAuth client ID an app ships must still exist in Google.

Collects client IDs from the app config (lib/flavor_config.dart, android/**/google-services.json,
ios/**/GoogleService-Info.plist, web/index.html) and asks Google's sign-in endpoint about each one.
Google answers `deleted_client` for a deleted client and `invalid_client` for an unknown one; both fail
the gate. Any other answer (the sign-in page, redirect_uri_mismatch, ...) means the client exists.
Why: Japa 1.17.0 shipped a deleted login client and every Google sign-in failed on the phone.
Client IDs are public identifiers (PUBLIC-ID-1); nothing secret is read or printed.

  google_client_live_gate.py [--root DIR] [--demo]
Exit 0: all live. Exit 1: a deleted or unknown client. Exit 2: Google could not be reached (fails closed).
"""
import argparse, base64, glob, os, re, sys, time, urllib.error, urllib.parse, urllib.request

ID_RE = re.compile(r"\b(\d{9,14}-[a-z0-9]{32})\.apps\.googleusercontent\.com")
PROBE_REDIRECT = "https://mypurpus.com/orbit-auth-service/api/google/callback"
FILES = ["lib/flavor_config.dart", "android/**/google-services.json", "ios/**/GoogleService-Info.plist", "web/index.html"]
DEAD = {"deleted_client": "deleted in Google", "invalid_client": "not found in Google"}


def collect(root):
    found = {}
    for pat in FILES:
        for path in sorted(glob.glob(os.path.join(root, pat), recursive=True)):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in ID_RE.finditer(text):
                found.setdefault(m.group(1), set()).add(os.path.relpath(path, root))
    return found


def decode_auth_error(url):
    """Google redirects to .../signin/oauth/error?authError=<base64 protobuf>; the first field is the code."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    raw = (q.get("authError") or [""])[0]
    if not raw:
        return None
    raw += "=" * (-len(raw) % 4)
    try:
        b = base64.urlsafe_b64decode(raw)
    except Exception:
        return "unreadable"
    m = re.match(rb"\n.([a-z_]+)", b, re.S)
    return m.group(1).decode() if m else "unreadable"


def fetch_final_url(client_id, opener=None):
    url = ("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id + ".apps.googleusercontent.com", "redirect_uri": PROBE_REDIRECT,
        "response_type": "code", "scope": "openid email"}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (orbit google-client-live-gate)"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as r:
        return r.geturl()


def probe(client_id, fetch=fetch_final_url, tries=3):
    last = None
    for i in range(tries):
        try:
            return decode_auth_error(fetch(client_id)) or "live"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (i + 1))
    return "unreachable:" + type(last).__name__


def run(root, fetch=fetch_final_url, out=print):
    ids = collect(root)
    if not ids:
        out("google-client-live-gate: no Google client IDs found under " + root + " (nothing to check)")
        return 0
    bad = unreachable = 0
    out("| Client ID | Answer | Found in |\n|---|---|---|")
    for cid in sorted(ids):
        ans = probe(cid, fetch)
        where = ", ".join(sorted(ids[cid]))
        if ans in DEAD:
            bad += 1
            out(f"| `{cid}` | **{ans}: {DEAD[ans]}** | {where} |")
        elif ans.startswith("unreachable"):
            unreachable += 1
            out(f"| `{cid}` | **{ans}** | {where} |")
        else:
            out(f"| `{cid}` | live | {where} |")
    if bad:
        out(f"\nFAIL: {bad} client ID(s) no longer exist in Google. Point the app at a live client "
            "(GOOGLE-CLIENT-1, coding-rules gcp/google-cloud-admin.md) or restore it in the console within 30 days.")
        return 1
    if unreachable:
        out(f"\nFAIL: Google could not be reached for {unreachable} ID(s); the gate refuses to pass unchecked.")
        return 2
    out(f"\nPASS: all {len(ids)} client ID(s) are live in Google.")
    return 0


def demo():
    import tempfile
    time.sleep = lambda *_: None
    live_url = "https://accounts.google.com/signin/oauth/error?authError=" + base64.urlsafe_b64encode(b"\n\x15redirect_uri_mismatch\x12x").decode()
    dead_url = "https://accounts.google.com/signin/oauth/error?authError=" + base64.urlsafe_b64encode(b"\n\x0edeleted_client\x12x").decode()
    gone_url = "https://accounts.google.com/signin/oauth/error?authError=" + base64.urlsafe_b64encode(b"\n\x0einvalid_client\x12x").decode()
    a, d, g = "502053789507-" + "a" * 32, "502053789507-" + "d" * 32, "502053789507-" + "g" * 32
    answers = {a: live_url, d: dead_url, g: gone_url}
    fake = lambda cid: answers[cid]
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name)
        ok = ok and cond
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, "lib")); os.makedirs(os.path.join(t, "android/app/src/prod"))
        open(os.path.join(t, "lib/flavor_config.dart"), "w").write(f"const x = '{a}.apps.googleusercontent.com';")
        check("all live passes", run(t, fake, out=lambda *_: None) == 0)
        open(os.path.join(t, "android/app/src/prod/google-services.json"), "w").write(f'{{"client_id":"{d}.apps.googleusercontent.com"}}')
        check("a deleted client fails", run(t, fake, out=lambda *_: None) == 1)
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, "lib"))
        open(os.path.join(t, "lib/flavor_config.dart"), "w").write(f"'{g}.apps.googleusercontent.com'")
        check("an unknown client fails", run(t, fake, out=lambda *_: None) == 1)
        def down(cid): raise urllib.error.URLError("down")
        check("unreachable Google fails closed", run(t, lambda c: down(c), out=lambda *_: None) == 2)
        check("unreachable probe answers unreachable", probe(g, down, tries=1).startswith("unreachable"))
    check("no IDs passes", run(tempfile.mkdtemp(), fake, out=lambda *_: None) == 0)
    check("live answer decodes", decode_auth_error(live_url) == "redirect_uri_mismatch")
    check("no authError means live", decode_auth_error("https://accounts.google.com/v3/signin/identifier?x=1") is None)
    print("google client live gate self-test: " + ("GREEN" if ok else "RED"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    sys.exit(demo() if a.demo else run(a.root))
