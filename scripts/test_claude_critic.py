#!/usr/bin/env python3
"""Self-test for scripts/claude_critic.py: the gates (G2-G5), label removal, model choice and the pack budget.
A fake GitHub and a fake API; no network. Run: python3 scripts/test_claude_critic.py"""
import datetime, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_critic as C  # noqa: E402

NOW = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.timezone.utc)
SHA = "a" * 40


def verdict(day="2026-11-03", sha=SHA):
    return {"user": {"login": "github-actions[bot]"}, "created_at": f"{day}T08:00:00Z",
            "body": f"{C.HEAD} {sha[:7]}\n\n**Verdict: GREEN**\n\n_Reviewed head: {sha}_"}


class FakeGH:
    def __init__(self, pr_comments=(), repo_comments=(), diff="+ a line\n", body="Spec: `orchestration/x.md`"):
        self.pr_comments, self.repo_comments, self.diff, self.body = list(pr_comments), list(repo_comments), diff, body
        self.posted, self.deleted = [], []

    def __call__(self, path, method="GET", body=None, accept=None):
        if method == "POST":
            self.posted.append(body["body"]); return {}
        if method == "DELETE":
            self.deleted.append(path); return {}
        if path.endswith("/pulls/7") and accept:
            return self.diff
        if path.endswith("/pulls/7"):
            return {"title": "t", "body": self.body, "user": {"login": "u"}, "base": {"ref": "main", "repo": {"default_branch": "main"}},
                    "head": {"ref": "b", "sha": SHA}}
        if "/issues/7/comments" in path:
            return self.pr_comments if "page=1" in path else []
        if "/issues/comments?since=" in path:
            return self.repo_comments if "page=1" in path else []
        if "/pulls/7/files" in path:
            return []
        raise AssertionError(path)


def env(**kw):
    e = {"REPO": "purpusgit/specs", "PR": "7", "GH_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-test", "DAILY_CAP": "3",
         "EVENT_ACTION": "labeled", "EVENT_LABEL": "critic", "DEEP": "false"}
    e.update(kw)
    return e


def ok_api(calls, text="**Verdict: GREEN**\n1. MINOR x"):
    def api(body, key):
        calls.append(body)
        return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "end_turn"}, None
    return api


def spec_fetch(path, repo="coding-rules"):
    return "the spec" if repo == "specs" else "pack text"


class Gates(unittest.TestCase):
    def run_main(self, gh, calls, api=None, fetch=spec_fetch, **kw):
        C.main(env(**kw), gh=gh, pack_fetch=fetch, api=api or ok_api(calls), now=NOW)

    def test_one_label_one_call_then_label_removed(self):
        gh, calls = FakeGH(), []
        self.run_main(gh, calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "claude-sonnet-5-5")
        self.assertIn("**Verdict: GREEN**", gh.posted[-1])
        self.assertTrue(gh.deleted and gh.deleted[0].endswith("/labels/critic"))
        self.assertEqual(calls[0]["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_deep_uses_opus(self):
        gh, calls = FakeGH(), []
        self.run_main(gh, calls, DEEP="true", EVENT_LABEL="critic-deep")
        self.assertEqual(calls[0]["model"], "claude-opus-5-5")
        self.assertTrue(gh.deleted[0].endswith("/labels/critic-deep"))

    def test_g2_fourth_review_skips(self):
        gh, calls = FakeGH(pr_comments=[verdict()] * 3), []
        self.run_main(gh, calls)
        self.assertEqual(calls, [])
        self.assertIn("round cap reached (3/3)", gh.posted[-1])
        self.assertNotIn("**Verdict:", gh.posted[-1])          # a skip is not counted as a verdict
        self.assertTrue(gh.deleted)                             # the label still comes off

    def test_g2_skips_and_other_bots_do_not_count(self):
        other = {"user": {"login": "someone"}, "body": f"{C.HEAD} x\n**Verdict: GREEN**"}
        skip = {"user": {"login": "github-actions[bot]"}, "body": f"{C.HEAD} skipped at abc\n\nround cap"}
        gh, calls = FakeGH(pr_comments=[verdict(), verdict(), other, skip]), []
        self.run_main(gh, calls)
        self.assertEqual(len(calls), 1)

    def test_g3_daily_budget(self):
        gh, calls = FakeGH(repo_comments=[verdict()] * 3), []
        self.run_main(gh, calls)
        self.assertEqual(calls, [])
        self.assertIn("daily review budget reached (3/3", gh.posted[-1])

    def test_g3_counts_only_today(self):
        gh, calls = FakeGH(repo_comments=[verdict(day="2026-11-02")] * 5), []
        self.run_main(gh, calls)
        self.assertEqual(len(calls), 1)

    def test_g3_specs_budget_15(self):
        gh, calls = FakeGH(repo_comments=[verdict()] * 14), []
        self.run_main(gh, calls, DAILY_CAP="15")
        self.assertEqual(len(calls), 1)

    def test_g4_big_diff_not_sent(self):
        gh, calls = FakeGH(diff="x" * (C.DIFF_LIMIT + 1)), []
        self.run_main(gh, calls)
        self.assertEqual(calls, [])
        self.assertIn("over the 60,000 limit", gh.posted[-1])

    def test_g5_pool_empty_skips_with_fallback(self):
        gh, calls = FakeGH(), []
        self.run_main(gh, calls, KEY_EXHAUSTED="true")
        self.assertEqual(calls, [])
        self.assertIn("no credit", gh.posted[-1])
        self.assertIn("session-critic", gh.posted[-1])

    def test_g5_credit_runs_out_mid_call(self):
        gh, calls = FakeGH(), []
        def api(body, key):
            calls.append(body)
            return None, C.credit_skip(400, '{"error":{"message":"Your credit balance is too low"}}')
        self.run_main(gh, calls, api=api)
        self.assertIn("skipped: no credit (API status 400)", gh.posted[-1])

    def test_credit_skip_rules(self):
        self.assertTrue(C.credit_skip(401, ""))
        self.assertTrue(C.credit_skip(402, ""))
        self.assertTrue(C.credit_skip(400, "billing problem"))
        self.assertIsNone(C.credit_skip(400, "max_tokens too small"))
        self.assertIsNone(C.credit_skip(529, "overloaded"))

    def test_no_spec_named_caps_at_yellow(self):
        gh, calls = FakeGH(body="no spec here"), []
        self.run_main(gh, calls, SPEC_REPO="false")
        self.assertTrue(gh.posted[-1].split("\n\n", 1)[1].startswith("**Verdict: YELLOW**"))

    def test_spec_repo_needs_no_named_spec(self):
        gh, calls = FakeGH(body="no spec here"), []
        self.run_main(gh, calls, SPEC_REPO="true")
        self.assertIn("**Verdict: GREEN**", gh.posted[-1])

    def test_pack_trims_last_files_first(self):
        big = {p: ("y" * 40_000) for p in C.PACK_FILES}
        pack, loaded = C.build_pack(lambda p: big[p])
        self.assertLessEqual(len(pack), C.PACK_CHAR_BUDGET + 2_000)
        self.assertEqual(len(loaded), 3)                         # 07 and the decision log trimmed away
        self.assertNotIn("<<<PACK belongity-bible/07-architecture.md>>>", pack)
        self.assertIn("<<<PACK belongity-bible/README.md>>>", pack)


if __name__ == "__main__":
    unittest.main(verbosity=1)
