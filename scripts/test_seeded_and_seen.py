#!/usr/bin/env python3
"""Self-test for scripts/seeded_and_seen.py: a passing body, each missing part, the exemption, a draft, no screen files and
API errors, plus the workflow pins. A fake API; no network. Run: python3 scripts/test_seeded_and_seen.py"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import seeded_and_seen as S  # noqa: E402

PAT = S.DEFAULT_PATTERN
IMG = "![login](https://github.com/user-attachments/assets/4f1c0a52-8d3e-4b7a-9c61-0e2f5a7b3d19)"
GOOD = """Adds the new Home tab.

## TESTED
- Accounts: JAPA_TEST_SEEDED_A (member), JAPA_TEST_SEEDED_B
- Build: sandbox
- Seeded: org 4412, user 9987
- States:
  - Empty home
    %s
  - Home with 3 cards
    <img src="https://example.invalid/shots/cards.png" width="320">

## Notes
Nothing else.
""" % IMG
FILES = [{"filename": "lib/screens/home/home.screen.dart", "status": "modified"}]


def ev(body=GOOD, draft=False, labels=(), files=FILES, exists=lambda p, r: True):
    pr = {"body": body, "draft": draft, "labels": list(labels), "head_sha": "a" * 40}
    return S.evaluate(pr, files, PAT, exists)


def msgs(res):
    return " | ".join(res[1])


class Applicability(unittest.TestCase):
    def test_a_draft_passes_not_applicable(self):
        code, lines = ev(body="", draft=True)
        self.assertEqual(code, 0)
        self.assertIn("draft", lines[0])

    def test_no_screen_file_passes_not_applicable(self):
        code, lines = ev(body="", files=[{"filename": "lib/utils/x.dart", "status": "modified"}, {"filename": "README.md", "status": "added"}])
        self.assertEqual(code, 0)
        self.assertIn("no screen or widget file", lines[0])

    def test_screen_files_fire(self):
        for name in ("lib/screens/a/b.dart", "lib/widgets/card.dart", "lib/x/home_screen.dart", "lib/x/home_page.dart",
                     "lib/x/card_widget.dart", "lib/x/home.screen.dart", "lib/x/card.widget.dart"):
            self.assertTrue(S.fires([{"filename": name, "status": "added"}]), name)

    def test_other_files_and_pure_deletions_do_not_fire(self):
        for f in ({"filename": "lib/utils/screen_helper.dart", "status": "added"}, {"filename": "test/screens/a_test.dart", "status": "added"},
                  {"filename": "lib/screens/old.dart", "status": "removed"}, {"filename": "example/lib/screens/a.dart", "status": "added"}):
            self.assertFalse(S.fires([f]), f)

    def test_a_ui_pr_with_no_body_fails_closed(self):
        for body in (None, "", "   \n"):
            code, lines = ev(body=body)
            self.assertEqual(code, 1, repr(body))
            self.assertIn("no body", msgs((code, lines)))


class Requirements(unittest.TestCase):
    def test_a_good_body_passes(self):
        code, lines = ev()
        self.assertEqual((code, lines[0][:20]), (0, "seeded-and-seen pass"), msgs((code, lines)))

    def test_no_tested_section_fails(self):
        code, lines = ev(body="Adds the home tab. I tried it, it works.")
        self.assertEqual(code, 1)
        self.assertIn("no `## TESTED` section", msgs((code, lines)))

    def test_no_account_fails(self):
        code, lines = ev(body=GOOD.replace("JAPA_TEST_SEEDED_A (member), JAPA_TEST_SEEDED_B", "my own account"))
        self.assertEqual(code, 1)
        self.assertIn("no named test account", msgs((code, lines)))

    def test_a_password_or_token_next_to_the_account_fails(self):
        for tail in ("JAPA_TEST_SEEDED_A, password: hunter22x", "JAPA_TEST_SEEDED_A / token=abc123def", "JAPA_TEST_SEEDED_A eyJhbGciOiJIUzI1NiIsInR5cCI6.x",
                     "JAPA_TEST_SEEDED_A Xk29fj3Lq8Zr5Tn1Vb7Mc4Wd"):
            code, lines = ev(body=GOOD.replace("JAPA_TEST_SEEDED_A (member), JAPA_TEST_SEEDED_B", tail))
            self.assertEqual(code, 1, tail)
            self.assertIn("password or token", msgs((code, lines)), tail)

    def test_a_secret_on_the_line_below_the_account_fails(self):
        code, lines = ev(body=GOOD.replace("- Build: sandbox", "- Password: Zz9!pass-w0rd\n- Build: sandbox"))
        self.assertEqual(code, 1)
        self.assertIn("password or token", msgs((code, lines)))

    def test_urls_ids_and_account_names_alone_are_not_secrets(self):
        body = GOOD.replace("Seeded: org 4412, user 9987", "Seeded: org_8f3a9c2d1b7e4f60a1b2, user 9987")
        self.assertEqual(ev(body=body)[0], 0)

    def test_build_must_name_sandbox_or_dev(self):
        for b in ("- Build: prod", "- Build: my laptop", "- Build:", "- Build: development"):
            code, lines = ev(body=GOOD.replace("- Build: sandbox", b))
            self.assertEqual(code, 1, b)
            self.assertIn("`Build:`", msgs((code, lines)), b)
        self.assertEqual(ev(body=GOOD.replace("- Build: sandbox", "- **Build:** dev (japa_dev)"))[0], 0)
        self.assertEqual(ev(body=GOOD.replace("- Build: sandbox", "- Build: sandbox"))[0], 0)

    def test_no_build_line_fails(self):
        code, lines = ev(body=GOOD.replace("- Build: sandbox\n", ""))
        self.assertEqual(code, 1)
        self.assertIn("`Build:`", msgs((code, lines)))

    def test_seeded_ids_are_required(self):
        for repl in ("", "- Seeded:", "- Seeded: TODO", "- Seeded: n/a", "- Seeded: ..."):
            body = GOOD.replace("- Seeded: org 4412, user 9987", repl)
            code, lines = ev(body=body)
            self.assertEqual(code, 1, repr(repl))
            self.assertIn("`Seeded:`", msgs((code, lines)), repr(repl))

    def test_no_states_fails(self):
        code, lines = ev(body=GOOD[:GOOD.index("- States:")] + "\n## Notes\nx\n")
        self.assertEqual(code, 1)
        self.assertIn("no `States:` list", msgs((code, lines)))

    def test_every_state_needs_an_image(self):
        body = GOOD.replace('    <img src="https://example.invalid/shots/cards.png" width="320">', "    it looked right")
        code, lines = ev(body=body)
        self.assertEqual(code, 1)
        self.assertIn("state 2 (Home with 3 cards) has no image", msgs((code, lines)))
        self.assertNotIn("state 1", msgs((code, lines)))

    def test_a_bare_image_url_is_not_an_image(self):
        body = GOOD.replace(IMG, "https://example.invalid/a.png")
        code, lines = ev(body=body)
        self.assertEqual(code, 1)
        self.assertIn("state 1", msgs((code, lines)))

    def test_markdown_and_html_images_both_count(self):
        self.assertEqual(ev()[0], 0)  # state 1 markdown, state 2 <img>


class RepoPaths(unittest.TestCase):
    BODY = GOOD.replace(IMG, "`docs/evidence/empty-home.png`")

    def test_an_existing_repo_path_counts_at_the_pr_head(self):
        seen = []
        code, _ = ev(body=self.BODY, exists=lambda p, r: seen.append((p, r)) or True)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("docs/evidence/empty-home.png", "a" * 40)])

    def test_a_missing_repo_path_fails(self):
        code, lines = ev(body=self.BODY, exists=lambda p, r: False)
        self.assertEqual(code, 1)
        self.assertIn("docs/evidence/empty-home.png does not exist", msgs((code, lines)))

    def test_an_evidence_branch_is_the_ref_to_check(self):
        seen = []
        body = self.BODY.replace("- Build: sandbox", "- Build: sandbox\n- Evidence: web-visual/pr-7")
        code, _ = ev(body=body, exists=lambda p, r: seen.append(r) or True)
        self.assertEqual((code, seen), (0, ["web-visual/pr-7"]))

    def test_a_relative_markdown_image_is_checked_as_a_repo_path(self):
        seen = []
        body = GOOD.replace(IMG, "![x](docs/evidence/a.png)")
        code, _ = ev(body=body, exists=lambda p, r: seen.append(p) or True)
        self.assertEqual((code, seen), (0, ["docs/evidence/a.png"]))


class Exemption(unittest.TestCase):
    def test_label_and_reason_exempt(self):
        code, lines = ev(body="Rename only.\n\ntested-n/a: pure rename, no visible change", labels=["tested-n/a"])
        self.assertEqual(code, 0)
        self.assertIn("not machine-checked", lines[0].lower())
        self.assertIn("same account", lines[0])

    def test_the_label_without_a_reason_fails(self):
        code, lines = ev(body="Rename only.", labels=["tested-n/a"])
        self.assertEqual(code, 1)
        self.assertIn("no `tested-n/a: <reason>` line", msgs((code, lines)))

    def test_a_short_or_placeholder_reason_fails(self):
        for reason in ("tested-n/a: rename", "tested-n/a: <reason>", "tested-n/a:"):
            code, _ = ev(body="Rename only.\n" + reason, labels=["tested-n/a"])
            self.assertEqual(code, 1, reason)

    def test_the_reason_without_the_label_fails(self):
        code, lines = ev(body="Rename only.\ntested-n/a: pure rename, no visible change")
        self.assertEqual(code, 1)
        self.assertIn("label `tested-n/a` is not set", msgs((code, lines)))

    def test_the_exemption_does_not_paper_over_a_draft_or_other_labels(self):
        self.assertEqual(ev(body="x\ntested-n/a: pure rename, no visible change", labels=["bug"])[0], 1)


class Io(unittest.TestCase):
    def fake(self, files_status=200, files=None, contents_status=200):
        def get(path):
            if "/pulls/" in path and "/files" in path:
                return files_status, (files if files is not None else [{"filename": "lib/screens/a.dart", "status": "modified"}])
            if "/contents/" in path:
                return contents_status, {}
            raise AssertionError(path)
        return get

    def event(self, body=GOOD, draft=False, labels=()):
        return {"repository": {"full_name": "purpusgit/pkg_orbit_japa"},
                "pull_request": {"number": 7, "body": body, "draft": draft, "labels": [{"name": n} for n in labels], "head": {"sha": "b" * 40}}}

    def test_a_good_pr_passes_end_to_end(self):
        self.assertEqual(S.run(self.event(), PAT, self.fake())[0], 0)

    def test_a_draft_never_calls_the_api(self):
        def boom(path):
            raise AssertionError("a draft must not call the API: " + path)
        self.assertEqual(S.run(self.event(draft=True), PAT, boom)[0], 0)

    def test_a_files_api_error_is_a_gate_failure(self):
        with self.assertRaises(S.ApiError):
            S.run(self.event(), PAT, self.fake(files_status=500))

    def test_a_contents_api_error_is_a_gate_failure_but_404_is_a_normal_miss(self):
        body = RepoPaths.BODY
        with self.assertRaises(S.ApiError):
            S.run(self.event(body=body), PAT, self.fake(contents_status=403))
        code, lines = S.run(self.event(body=body), PAT, self.fake(contents_status=404))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", msgs((code, lines)))

    def test_more_than_100_files_are_paged(self):
        calls = []

        def get(path):
            calls.append(path)
            page = int(path.rsplit("page=", 1)[1])
            return 200, [{"filename": "docs/%d_%d.md" % (page, i), "status": "added"} for i in range(100 if page == 1 else 3)]
        out = S.changed_files("o/r", 1, get)
        self.assertEqual((len(out), len(calls)), (103, 2))

    def test_a_bad_pattern_or_an_event_without_a_pr(self):
        with self.assertRaises(S.GateError):
            S.run(self.event(), "JAPA_TEST_[", self.fake())
        self.assertEqual(S.run({"repository": {"full_name": "o/r"}}, PAT, self.fake())[0], 0)

    def test_the_pattern_is_per_repo(self):
        body = GOOD.replace("JAPA_TEST_SEEDED_A (member), JAPA_TEST_SEEDED_B", "BINDER_TEST_ONE")
        self.assertEqual(S.run(self.event(body=body), PAT, self.fake())[0], 1)
        self.assertEqual(S.run(self.event(body=body), r"BINDER_TEST_[A-Z]+", self.fake())[0], 0)

    def test_main_reads_the_event_file_writes_a_summary_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            evp, sm = os.path.join(d, "e.json"), os.path.join(d, "s.md")
            json.dump({"repository": {"full_name": "o/r"}, "pull_request": {"number": 1, "body": "x", "draft": True, "labels": []}}, open(evp, "w"))
            old = dict(os.environ)
            os.environ.update(GITHUB_EVENT_PATH=evp, GITHUB_STEP_SUMMARY=sm, GH_TOKEN="t")
            try:
                self.assertEqual(S.main(), 0)
                self.assertIn("cannot be machine-checked", open(sm).read())
                os.environ["GITHUB_EVENT_PATH"] = os.path.join(d, "missing.json")
                self.assertEqual(S.main(), 2)
            finally:
                os.environ.clear()
                os.environ.update(old)


class Workflows(unittest.TestCase):
    """The reusable and the standalone ruleset target run the same script, fetched the same way, with no PR code checked out."""

    @staticmethod
    def wf(name):
        import yaml
        with open(os.path.join(HERE, "..", ".github", "workflows", name)) as fh:
            return yaml.safe_load(fh)

    def steps(self, name, job="seeded-and-seen"):
        return self.wf(name)["jobs"][job]["steps"]

    def test_same_script_same_fetch_and_no_checkout_of_pr_code(self):
        a, b = self.steps("reusable-seeded-and-seen.yml"), self.steps("ux-required.yml")
        fetch = lambda ss: [s["with"] for s in ss if "checkout" in s.get("uses", "")]
        run = lambda ss: next(s["run"] for s in ss if "seeded_and_seen.py" in s.get("run", ""))
        self.assertEqual(fetch(a), fetch(b))
        self.assertEqual(len(fetch(b)), 1)  # only the script's own checkout: no PR code
        self.assertEqual(fetch(b)[0]["repository"], "purpusgit/.github")
        self.assertEqual(fetch(b)[0]["ref"], "main")
        self.assertEqual(fetch(b)[0]["sparse-checkout"].strip(), "scripts/seeded_and_seen.py")
        self.assertEqual(run(a), run(b))
        self.assertIn("python3 -I", run(b))

    def test_the_standalone_triggers_cover_a_body_edit_a_label_and_ready_for_review(self):
        types = self.wf("ux-required.yml")[True]["pull_request"]["types"]
        for t in ("opened", "edited", "synchronize", "reopened", "labeled", "unlabeled", "ready_for_review"):
            self.assertIn(t, types)
        self.assertIn("seeded-and-seen", self.wf("ux-required.yml")["jobs"])

    def test_the_account_pattern_is_per_repo(self):
        env = lambda ss: next(s["env"] for s in ss if "seeded_and_seen.py" in s.get("run", ""))
        self.assertIn("vars.SEEDED_TEST_ACCOUNT_PATTERN", env(self.steps("ux-required.yml"))["TEST_ACCOUNT_PATTERN"])
        self.assertIn("inputs.test_account_pattern", env(self.steps("reusable-seeded-and-seen.yml"))["TEST_ACCOUNT_PATTERN"])
        self.assertEqual(self.wf("reusable-seeded-and-seen.yml")[True]["workflow_call"]["inputs"]["test_account_pattern"]["default"], "")


if __name__ == "__main__":
    unittest.main(verbosity=1)
