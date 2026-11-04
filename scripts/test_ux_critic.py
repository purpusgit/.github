#!/usr/bin/env python3
"""Self-test for scripts/ux_critic.py: refusals, screen scope, the cap, the comment. Fake GitHub and API; no network.
Run: python3 scripts/test_ux_critic.py"""
import json, os, sys, tempfile, unittest, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ux_critic as U  # noqa: E402

HEAD_MANIFEST = "package: p\nscreens:\n  AScreen:\n    file: lib/a.dart\n  BScreen:\n    file: lib/b.dart\n  NewScreen:\n    file: lib/n.dart\n"
BASE_MANIFEST = "package: p\nscreens:\n  AScreen:\n    file: lib/a.dart\n  BScreen:\n    file: lib/b.dart\n"


class FakeGH:
    def __init__(self, files=(), comments=(), base=BASE_MANIFEST):
        self.files, self.comments, self.base = list(files), list(comments), base
        self.posted, self.patched = [], []

    def __call__(self, path, method="GET", body=None, accept=None):
        if method == "POST":
            self.posted.append(body["body"]); return {}
        if method == "PATCH":
            self.patched.append((path, body["body"])); return {}
        if path.endswith("/pulls/7"):
            return {"head": {"sha": "h" * 40}, "base": {"sha": "b" * 40}}
        if "/contents/" in path:
            return HEAD_MANIFEST if "ref=h" in path else self.base
        if "/pulls/7/files" in path:
            return self.files if "page=1" in path else []
        if "/issues/7/comments" in path:
            return self.comments if "page=1" in path else []
        raise AssertionError(path)


def reply(findings, usage=None, stop="end_turn"):
    return {"stop_reason": stop, "usage": usage or {"input_tokens": 10_000, "output_tokens": 500},
            "content": [{"type": "text", "text": json.dumps({"findings": findings})}]}, None


class Critic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shots = []
        for cls in ("AScreen", "BScreen", "NewScreen"):
            for w, loc in ((360, "en"), (601, "en"), (1366, "en"), (1920, "en"), (360, "te"), (1366, "te"), (800, "en")):
                png = f"{cls}@{w}x800_{loc}_loaded.png"
                with open(os.path.join(self.dir, png), "wb") as fh:
                    fh.write(b"\x89PNG fake")
                shots.append({"class": cls, "width": w, "height": 800, "locale": loc, "state": "loaded", "png": png, "texts": ["Hi"]})
        self.critic({"harness_version": 1, "fonts_ok": True, "shots": shots})
        self.calls = []

    def critic(self, data):
        with open(os.path.join(self.dir, "critic.json"), "w") as fh:
            json.dump(data, fh)

    def env(self, **kw):
        e = {"REPO": "purpusgit/pkg_orbit_client_core", "PR": "7", "GH_TOKEN": "x", "ANTHROPIC_API_KEY": "k", "SHOTS_DIR": self.dir}
        e.update(kw)
        return e

    def api(self, result_fn):
        def call(body, key):
            self.calls.append(body)
            return result_fn(body)
        return call

    def run_main(self, gh, api=None, specs=lambda p: "RUBRIC", **kw):
        U.main(self.env(**kw), gh=gh, specs=specs, api=api or self.api(lambda b: reply([])))
        return (gh.posted + [b for _, b in gh.patched])[-1]

    def test_noop_pr_is_a_tick_line_and_no_calls(self):
        gh = FakeGH(base=HEAD_MANIFEST, files=[{"filename": "README.md", "additions": 3}])
        body = self.run_main(gh)
        self.assertEqual(self.calls, [])
        self.assertTrue(body.startswith(f"{U.HEAD}: ✅"))

    def test_refuses_without_critic_json(self):
        os.remove(os.path.join(self.dir, "critic.json"))
        body = self.run_main(FakeGH())
        self.assertIn("no critic.json", body)
        self.assertEqual(self.calls, [])

    def test_refuses_when_fonts_not_ok(self):
        self.critic({"fonts_ok": False, "shots": []})
        self.assertIn("fonts_ok is false", self.run_main(FakeGH()))

    def test_refuses_without_rubric(self):
        def no_rubric(path):
            raise urllib.error.HTTPError(path, 404, "nf", {}, None)
        self.assertIn("rubric", self.run_main(FakeGH(), specs=no_rubric))

    def test_empty_pool_skips(self):
        self.assertIn("no credit", self.run_main(FakeGH(), KEY_EXHAUSTED="true"))
        self.assertEqual(self.calls, [])

    def test_scope_is_changed_and_new_screens_with_vision_sizes(self):
        gh = FakeGH(files=[{"filename": "lib/b.dart", "additions": 5}])
        self.run_main(gh)
        judged = [c["messages"][0]["content"][-1]["text"].split(".")[0] for c in self.calls]
        self.assertEqual(judged, ["Screen NewScreen", "Screen BScreen"])      # new row first, A untouched
        imgs = [b for b in self.calls[0]["messages"][0]["content"] if b["type"] == "image"]
        self.assertEqual(len(imgs), 5)                                        # 360/601/1366/1920 en + te at 360
        self.assertEqual(self.calls[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(self.calls[0]["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_screen_limit_is_noted(self):
        gh = FakeGH(files=[{"filename": "lib/a.dart"}, {"filename": "lib/b.dart"}])
        body = self.run_main(gh, MAX_SCREENS="1")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("not judged (limit 1)", body)

    def test_cap_stops_calls(self):
        gh = FakeGH(files=[{"filename": "lib/a.dart"}, {"filename": "lib/b.dart"}])
        big = {"input_tokens": 1_000_000, "output_tokens": 0}            # $2.00 per call
        body = self.run_main(gh, api=self.api(lambda b: reply([], usage=big)), CAP_USD="3")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("stopped at the $3.00 cap", body)

    def test_findings_collapse_across_sizes_fail_first(self):
        f = lambda size, sev, rid: {"class": "x", "size": size, "state": "loaded", "rubric_id": rid,  # noqa: E731
                                     "severity": sev, "what": "w", "smallest_fix": "s"}
        gh = FakeGH(base=HEAD_MANIFEST, files=[{"filename": "lib/a.dart"}])
        body = self.run_main(gh, api=self.api(lambda b: reply([f("360x800", "WARN", "R-V2"), f("1366x800", "WARN", "R-V2"), f("1920x800", "FAIL", "R-V4")])))
        rows = [ln for ln in body.splitlines() if ln.startswith("| FAIL") or ln.startswith("| WARN")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("| FAIL"))
        self.assertIn("1366x800, 360x800", rows[1])

    def test_credit_error_mid_run_skips(self):
        gh = FakeGH(files=[{"filename": "lib/a.dart"}])
        body = self.run_main(gh, api=lambda b, k: (None, "skipped: no credit (API status 402); x."))
        self.assertIn("skipped: no credit", body)

    def test_comment_is_updated_in_place(self):
        old = {"id": 99, "user": {"login": "github-actions[bot]"}, "body": f"{U.HEAD}: old"}
        gh = FakeGH(base=HEAD_MANIFEST, comments=[old])
        self.run_main(gh)
        self.assertEqual(gh.posted, [])
        self.assertTrue(gh.patched[0][0].endswith("/issues/comments/99"))

    def test_cost_prices(self):
        self.assertAlmostEqual(U.cost({"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000}), 3.2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
