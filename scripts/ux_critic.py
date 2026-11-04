#!/usr/bin/env python3
"""The UX critic (specs flutter-ui/ux-critic-agent-spec.md §5): one Claude vision call per changed screen,
one PR comment updated in place. Advisory, never a gate.

Reads the W-WIDTH artifact (client_core #884): build/web_visual/critic.json + the PNGs it names.
  - Refuses with one line when critic.json is missing, fonts_ok is false, or the rubric is missing.
  - v1 scope: screens whose manifest row is new or whose `file:` the PR touches, most-changed first,
    at most MAX_SCREENS. A no-op PR therefore gets the ✅ line. No base comparison yet (§3): "no base".
  - A hard USD cap per PR from reported usage; an empty key pool or a credit error posts "skipped".

Env: REPO, PR, GH_TOKEN (comments), PACK_TOKEN (reads specs), ANTHROPIC_API_KEY, KEY_EXHAUSTED,
     SHOTS_DIR (build/web_visual), RUBRIC_PATH, MAX_SCREENS (15), CAP_USD (3), MODEL.
Test: python3 scripts/test_ux_critic.py
"""
import base64, json, os, sys, urllib.error, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_critic import call_api, make_gh, paged  # noqa: E402
from central_ui_gate import MANIFEST, parse_manifest  # noqa: E402

HEAD = "### UX critic"
SIZES = (360, 601, 1366, 1920)          # §4 "What vision is shown": loaded state, plus te at 360
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Sonnet 5.5 list prices, USD per token: input, cache write (1.25x), cache read, output.
PRICE = {"input": 2.00e-6, "cache_write": 2.50e-6, "cache_read": 0.20e-6, "output": 10.00e-6}
MAX_TOKENS = 8000
FINDING = {
    "type": "object", "additionalProperties": False,
    "required": ["class", "size", "state", "rubric_id", "severity", "what", "smallest_fix"],
    "properties": {"class": {"type": "string"}, "size": {"type": "string"}, "state": {"type": "string"},
                   "rubric_id": {"type": "string"}, "severity": {"type": "string", "enum": ["FAIL", "WARN"]},
                   "what": {"type": "string"}, "smallest_fix": {"type": "string"}},
}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["findings"],
          "properties": {"findings": {"type": "array", "items": FINDING}}}


class Refuse(Exception):
    """The job cannot judge this PR; the message is the one line it posts."""


def cost(usage):
    return (usage.get("input_tokens", 0) * PRICE["input"]
            + usage.get("cache_creation_input_tokens", 0) * PRICE["cache_write"]
            + usage.get("cache_read_input_tokens", 0) * PRICE["cache_read"]
            + usage.get("output_tokens", 0) * PRICE["output"])


def load_critic(shots_dir):
    path = os.path.join(shots_dir, "critic.json")
    if not os.path.exists(path):
        raise Refuse("not run: W-WIDTH produced no critic.json (run it with WEB_VISUAL_SHOTS=1).")
    with open(path) as fh:
        data = json.load(fh)
    if data.get("fonts_ok") is not True:
        raise Refuse("not run: fonts_ok is false, so the shots used fallback fonts and can't be judged.")
    return data


def pick_screens(files, head_rows, base_rows, limit):
    """[(class, churn)] for rows new on the PR or whose file the PR changed, most-changed first."""
    churn = {f["filename"]: f.get("additions", 0) + f.get("deletions", 0) for f in files}
    picked = []
    for cls, row in head_rows.items():
        if cls not in base_rows or row.get("file") in churn:
            picked.append((cls, churn.get(row.get("file"), 0) + (1_000_000 if cls not in base_rows else 0)))
    picked.sort(key=lambda p: (-p[1], p[0]))
    return [c for c, _ in picked[:limit]], [c for c, _ in picked[limit:]]


def shots_for(cls, critic):
    mine = [s for s in critic.get("shots", []) if s.get("class") == cls and s.get("state", "loaded") == "loaded"]
    chosen = [s for s in mine if s.get("width") in SIZES and (not str(s.get("locale", "")).startswith("te") or s.get("width") == 360)]
    return chosen or mine[:5]


def image_blocks(shots, shots_dir):
    blocks, skipped = [], []
    for s in shots:
        p = os.path.join(shots_dir, os.path.basename(s.get("png", "")))
        if not os.path.isfile(p) or os.path.getsize(p) > MAX_IMAGE_BYTES:
            skipped.append(s.get("png", "?"))
            continue
        with open(p, "rb") as fh:
            data = base64.b64encode(fh.read()).decode()
        blocks.append({"type": "text", "text": f"{s['class']} @ {s.get('width')}x{s.get('height')}, locale {s.get('locale')}, state {s.get('state', 'loaded')}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    return blocks, skipped


def request(rubric, cls, shots, blocks, model):
    context = [{k: s.get(k) for k in ("width", "height", "locale", "state", "texts", "rects") if k in s} for s in shots]
    return {
        "model": model, "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": rubric, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": blocks + [{"type": "text", "text":
                      f"Screen {cls}. Harness data per shot (visible texts, rects):\n{json.dumps(context)[:20000]}\n\n"
                      "Judge only the vision-owned rubric items. Report findings as JSON; an empty list when nothing is wrong. "
                      "size is WxH as labelled."}]}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def render(findings, sha, notes):
    if not findings:
        return f"{HEAD}: ✅ no findings at {sha[:7]} (no base)." + "".join(f"\n- {n}" for n in notes)
    rows = {}
    for f in findings:   # the same finding across sizes is one row
        key = (f["class"], f["state"], f["rubric_id"], f["severity"])
        r = rows.setdefault(key, {**f, "sizes": []})
        r["sizes"].append(f["size"])
    ordered = sorted(rows.values(), key=lambda r: (r["severity"] != "FAIL", r["class"], r["rubric_id"]))
    line = lambda r: (f"| {r['severity']} | `{r['class']}` | {', '.join(sorted(set(r['sizes'])))} | {r['rubric_id']} | "  # noqa: E731
                      f"{r['what'].replace('|', '/')} | {r['smallest_fix'].replace('|', '/')} |")
    head = "| Severity | Screen | Sizes | Rubric | What | Smallest fix |\n|---|---|---|---|---|---|"
    out = [f"{HEAD}: {len(ordered)} finding(s) at {sha[:7]} (no base, advisory)", "", head] + [line(r) for r in ordered[:10]]
    if len(ordered) > 10:
        out += ["", f"<details><summary>{len(ordered) - 10} more</summary>", "", head] + [line(r) for r in ordered[10:]] + ["", "</details>"]
    return "\n".join(out + [""] + [f"- {n}" for n in notes])


def upsert(gh, repo, pr, body):
    """One comment, updated in place."""
    mine = [c for c in paged(gh, f"/repos/{repo}/issues/{pr}/comments")
            if c.get("user", {}).get("login") == "github-actions[bot]" and c.get("body", "").startswith(HEAD)]
    if mine:
        gh(f"/repos/{repo}/issues/comments/{mine[-1]['id']}", method="PATCH", body={"body": body})
    else:
        gh(f"/repos/{repo}/issues/{pr}/comments", method="POST", body={"body": body})


def main(env=os.environ, gh=None, specs=None, api=call_api):
    repo, pr = env["REPO"], env["PR"]
    gh = gh or make_gh(env["GH_TOKEN"])
    if specs is None:
        sg = make_gh(env.get("PACK_TOKEN") or env["GH_TOKEN"])
        specs = lambda path: sg(f"/repos/purpusgit/specs/contents/{urllib.parse.quote(path)}?ref=main", accept="application/vnd.github.raw")  # noqa: E731
    shots_dir = env.get("SHOTS_DIR") or "build/web_visual"
    cap, limit = float(env.get("CAP_USD") or 3), int(env.get("MAX_SCREENS") or 15)
    meta = gh(f"/repos/{repo}/pulls/{pr}")
    sha = meta["head"]["sha"]
    notes, findings, spent = [], [], 0.0
    try:
        critic = load_critic(shots_dir)
        try:
            rubric = specs(env.get("RUBRIC_PATH") or "flutter-ui/ux-critic/rubric.md")
        except urllib.error.HTTPError:
            raise Refuse("not run: the rubric (specs flutter-ui/ux-critic/rubric.md) is missing or unreadable.")
        if env.get("KEY_EXHAUSTED") == "true" or not env.get("ANTHROPIC_API_KEY"):
            raise Refuse("skipped: no credit (every pooled key is out or unset).")
        raw = lambda ref: gh(f"/repos/{repo}/contents/{MANIFEST}?ref={ref}", accept="application/vnd.github.raw")  # noqa: E731
        try:
            head_rows = parse_manifest(raw(sha))
        except urllib.error.HTTPError:
            raise Refuse(f"not run: no {MANIFEST} at the PR head.")
        try:
            base_rows = parse_manifest(raw(meta["base"]["sha"]))
        except urllib.error.HTTPError:
            base_rows = {}
        screens, over = pick_screens(paged(gh, f"/repos/{repo}/pulls/{pr}/files"), head_rows, base_rows, limit)
        if over:
            notes.append(f"{len(over)} more changed screen(s) not judged (limit {limit}): {', '.join(over)}")
        worst = 0.30   # a call's worst case before any is measured: 5 shots and MAX_TOKENS of output
        for cls in screens:
            if spent + worst > cap:
                notes.append(f"stopped at the ${cap:.2f} cap; not judged: {', '.join(screens[screens.index(cls):])}")
                break
            shots = shots_for(cls, critic)
            blocks, bad = image_blocks(shots, shots_dir)
            if bad:
                notes.append(f"{cls}: shot(s) missing or over 5 MB, not sent: {', '.join(bad)}")
            if not blocks:
                notes.append(f"{cls}: no loaded shot in critic.json, not judged")
                continue
            reply, why = api(request(rubric, cls, shots, blocks, env.get("MODEL") or "claude-sonnet-5-5"), env["ANTHROPIC_API_KEY"])
            if why:
                raise Refuse(why)
            c = cost(reply.get("usage", {}))
            spent, worst = spent + c, max(worst, c * 1.5)
            print(f"{cls}: ${c:.4f}, total ${spent:.4f}")
            if reply.get("stop_reason") != "end_turn":
                notes.append(f"{cls}: no verdict (stop reason {reply.get('stop_reason')})")
                continue
            text = next((b["text"] for b in reply.get("content", []) if b.get("type") == "text"), "{}")
            findings += [{**f, "class": cls} for f in json.loads(text).get("findings", [])]
        notes.append(f"{len(screens)} screen(s) in scope, ${spent:.2f} of the ${cap:.2f} cap")
        body = render(findings, sha, notes)
    except Refuse as e:
        body = f"{HEAD}: {e}"
    print(body)
    upsert(gh, repo, pr, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
