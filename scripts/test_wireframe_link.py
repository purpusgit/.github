#!/usr/bin/env python3
"""Self-test for scripts/wireframe_link.py: the wireframe rows of CALM-1 spec section 7, with a fixture API (no network).
Run: python3 scripts/test_wireframe_link.py"""
import contextlib
import datetime
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wireframe_link as W  # noqa: E402

TODAY = datetime.date(2026, 11, 16)
BOOK = "lib/ui/bookings_screen.dart"
HOME = "lib/ui/home_screen.dart"


def manifest(*rows):
    out = "package: my_app\nscreens:\n"
    for name, file_, extra in rows:
        out += "  %s:\n    file: %s\n    cap: reading\n    build: \"const %s()\"\n" % (name, file_, name) + "".join("    %s\n" % e for e in extra)
    return out


MAN = manifest(("BookingsScreen", BOOK, []), ("HomeScreen", HOME, ["flavour_home: true"]),
               ("NewFlavourScreen", "lib/ui/nf.dart", ["flavour: nearyest", "new_until: 2027-01-01"]),
               ("OldFlavourScreen", "lib/ui/of.dart", ["flavour: belongity", "new_until: 2026-01-01"]))
FRAMES = "https://github.com/purpusgit/specs/blob/main/flutter-ui/frames/bookings.md"
LEDGER = "# approvals\nframes_file\tscreens\tkind\tdate\tlink\nflutter-ui/frames/bookings.md\tBookingsScreen\tfounder\t2026-11-10\thttps://x/1\n"


class Fake:
    """The API behind a function: fixtures instead of GitHub."""
    def __init__(self, files, base=MAN, ledger=LEDGER, boom=None):
        self.files, self.base, self.ledger, self.boom, self.calls = files, base, ledger, boom, []

    def pr_files(self, repo, number):
        if self.boom == "files":
            raise W.Unreadable("cannot read the PR's files (HTTP 500)")
        return self.files

    def contents(self, repo, path, ref, ledger=False):
        self.calls.append((repo, path, ref))
        if ledger:
            if self.ledger is None:
                raise W.Unreadable("cannot read ledger (HTTP 404)")
            return self.ledger
        if self.boom == "manifest":
            raise W.Unreadable("cannot read manifest (HTTP 500)")
        return self.base


def run(files, body, mode="warn", token="tok", **kw):
    api = Fake(files, **kw)
    env = {"CALM_MODE": mode, "REPO": "purpusgit/my_app", "PR_NUMBER": "7", "BASE_REF": "cwb", "PR_BODY": body, "SPECS_READ_TOKEN": token}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = W.main(env, api, TODAY)
    return code, out.getvalue(), api


def judge(files, body, ledger=LEDGER, base=MAN):
    return W.judge(base, set(files), body, ledger, "my_app", TODAY)


class Trigger(unittest.TestCase):
    def test_no_manifest_screen_changed_is_nothing_to_check(self):
        self.assertEqual(judge(["lib/util.dart"], "")["findings"], [])

    def test_a_changed_manifest_screen_with_no_line_is_a_finding(self):
        r = judge([BOOK], "just a description")
        self.assertEqual(len(r["findings"]), 1)
        self.assertIn("BookingsScreen", r["findings"][0])

    def test_no_manifest_at_the_base_is_a_notice_and_a_pass(self):
        r = judge([BOOK], "", base=None)
        self.assertEqual(r["findings"], [])
        self.assertIn("no web-width manifest", r["notice"])

    def test_the_base_manifest_decides_so_a_head_that_drops_the_row_changes_nothing(self):
        code, out, api = run([HOME], "", "enforce")
        self.assertEqual(code, 1)
        self.assertIn(("purpusgit/my_app", W.MANIFEST, "cwb"), api.calls)  # read at the BASE ref


class NoVisualChange(unittest.TestCase):
    def test_a_reason_passes_and_records_the_pending_pixel_compare(self):
        r = judge([BOOK], "No visual change: pure refactor of the state class")
        self.assertEqual(r["findings"], [])
        self.assertIn("pixel compare pending (harness piece)", r["pending"][0])

    def test_a_placeholder_or_empty_reason_does_not_count(self):
        for body in ("No visual change: <why>", "No visual change:", "No visual change: x", "No visual change"):
            self.assertEqual(len(judge([BOOK], body)["findings"]), 1, body)


class Wireframe(unittest.TestCase):
    def test_a_good_founder_row_passes(self):
        self.assertEqual(judge([BOOK], "Wireframe: " + FRAMES)["findings"], [])
        self.assertEqual(judge([BOOK], "- Wireframe: " + FRAMES + "\n")["findings"], [])

    def test_a_file_not_in_the_ledger_fails(self):
        r = judge([BOOK], "Wireframe: https://github.com/purpusgit/specs/blob/main/flutter-ui/frames/other.md")
        self.assertEqual(len(r["findings"]), 1)
        self.assertIn("is not in flutter-ui/approvals.tsv", r["findings"][0])

    def test_a_ledger_row_for_a_different_screen_fails(self):
        ledger = LEDGER.replace("BookingsScreen", "SomethingElse")
        r = judge([BOOK], "Wireframe: " + FRAMES, ledger)
        self.assertEqual(len(r["findings"]), 1)
        self.assertIn("lists other screens", r["findings"][0])

    def test_repo_colon_class_in_the_ledger_matches(self):
        self.assertEqual(judge([BOOK], "Wireframe: " + FRAMES, LEDGER.replace("BookingsScreen", "my_app:BookingsScreen"))["findings"], [])

    def test_a_link_that_is_not_a_specs_main_link_fails(self):
        for link in ("https://example.com/x.md", "https://github.com/purpusgit/specs/blob/some-branch/flutter-ui/frames/bookings.md",
                     "https://github.com/other/specs/blob/main/flutter-ui/frames/bookings.md"):
            self.assertEqual(len(judge([BOOK], "Wireframe: " + link)["findings"]), 1, link)

    def test_a_delegated_row_on_a_flavour_home_fails_and_a_founder_row_passes(self):
        ledger = "flutter-ui/frames/home.md\tHomeScreen\tdelegated\t2026-11-10\thttps://x/2\n"
        link = "https://github.com/purpusgit/specs/blob/main/flutter-ui/frames/home.md"
        r = judge([HOME], "Wireframe: " + link, ledger)
        self.assertEqual(len(r["findings"]), 1)
        self.assertIn("flavour home", r["findings"][0])
        self.assertEqual(judge([HOME], "Wireframe: " + link, ledger.replace("delegated", "founder"))["findings"], [])

    def test_a_delegated_row_on_a_new_flavour_fails_until_new_until_has_passed(self):
        link = "https://github.com/purpusgit/specs/blob/main/flutter-ui/frames/f.md"
        for name, file_, bad in (("NewFlavourScreen", "lib/ui/nf.dart", True), ("OldFlavourScreen", "lib/ui/of.dart", False)):
            ledger = "flutter-ui/frames/f.md\t%s\tdelegated\t2026-11-10\thttps://x/3\n" % name
            self.assertEqual(len(judge([file_], "Wireframe: " + link, ledger)["findings"]), 1 if bad else 0, name)

    def test_a_delegated_row_on_an_ordinary_screen_passes(self):
        self.assertEqual(judge([BOOK], "Wireframe: " + FRAMES, LEDGER.replace("founder", "delegated"))["findings"], [])

    def test_an_unknown_kind_does_not_approve(self):
        self.assertEqual(len(judge([BOOK], "Wireframe: " + FRAMES, LEDGER.replace("founder", "verbal"))["findings"]), 1)

    def test_one_row_must_list_every_changed_screen(self):
        two = [BOOK, "lib/ui/of.dart"]
        link = "https://github.com/purpusgit/specs/blob/main/flutter-ui/frames/f.md"
        split = "flutter-ui/frames/f.md\tBookingsScreen\tfounder\td\tl\nflutter-ui/frames/f.md\tOldFlavourScreen\tfounder\td\tl\n"
        self.assertIn("no single ledger row", judge(two, "Wireframe: " + link, split)["findings"][0])
        both = "flutter-ui/frames/f.md\tBookingsScreen, OldFlavourScreen\tfounder\td\tl\n"
        self.assertEqual(judge(two, "Wireframe: " + link, both)["findings"], [])

    def test_parse_ledger_skips_comments_header_and_blank_lines_and_takes_a_full_link(self):
        rows = W.parse_ledger("# c\n\nframes_file\tscreens\tkind\n" + FRAMES + "\tA, my_app:B\tFounder\t1\t2\n")
        self.assertEqual(rows, [{"file": "flutter-ui/frames/bookings.md", "screens": {"A", "my_app:B"}, "kind": "founder"}])


class Main(unittest.TestCase):
    def test_warn_prints_the_finding_and_exits_0_enforce_exits_1(self):
        code, out, _ = run([BOOK], "", "warn")
        self.assertEqual(code, 0)
        self.assertIn("::warning title=ux-wireframe-link::", out)
        code, out, _ = run([BOOK], "", "enforce")
        self.assertEqual(code, 1)
        self.assertIn("::error title=ux-wireframe-link::", out)

    def test_a_good_pr_passes_in_both_modes_and_reads_the_ledger_from_specs_main(self):
        for mode in ("warn", "enforce"):
            code, out, api = run([BOOK], "Wireframe: " + FRAMES, mode)
            self.assertEqual(code, 0, out)
            self.assertIn(("purpusgit/specs", "flutter-ui/approvals.tsv", "main"), api.calls)

    def test_a_missing_specs_token_is_a_notice_and_a_pass_in_warn_and_fails_closed_in_enforce(self):
        code, out, api = run([BOOK], "Wireframe: " + FRAMES, "warn", token="")
        self.assertEqual(code, 0)
        self.assertIn("ledger unreadable: specs read access not set up (specs#449)", out)
        self.assertNotIn(("purpusgit/specs", "flutter-ui/approvals.tsv", "main"), api.calls)
        self.assertEqual(run([BOOK], "Wireframe: " + FRAMES, "enforce", token="")[0], 2)

    def test_an_unreadable_ledger_with_a_token_behaves_the_same(self):
        self.assertEqual(run([BOOK], "Wireframe: " + FRAMES, "warn", ledger=None)[0], 0)
        self.assertEqual(run([BOOK], "Wireframe: " + FRAMES, "enforce", ledger=None)[0], 2)

    def test_unreadable_files_or_base_manifest_fail_closed_in_every_mode(self):
        for boom in ("files", "manifest"):
            for mode in ("warn", "enforce"):
                self.assertEqual(run([BOOK], "No visual change: refactor", mode, boom=boom)[0], 2, (boom, mode))

    def test_the_ledger_is_not_fetched_when_no_wireframe_line_is_there(self):
        _, _, api = run([BOOK], "No visual change: refactor")
        self.assertFalse([c for c in api.calls if c[0] == "purpusgit/specs"])


class Workflows(unittest.TestCase):
    @staticmethod
    def load(name):
        import yaml
        with open(os.path.join(HERE, "..", ".github", "workflows", name), encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def job(self, name):
        wf = self.load(name)
        return wf, wf["jobs"]["ux-wireframe-link"]

    def test_both_run_the_same_script_isolated_from_main_with_the_body_in_the_environment(self):
        seen = []
        for name in ("ux-wireframe-link.yml", "reusable-ux-wireframe-link.yml"):
            wf, job = self.job(name)
            fetch, run_step = job["steps"][0]["with"], job["steps"][1]
            self.assertEqual((fetch["repository"], fetch["ref"]), ("purpusgit/.github", "main"))
            self.assertEqual(set(fetch["sparse-checkout"].split()), {"scripts/wireframe_link.py", "scripts/calm_static.py"})
            self.assertEqual(run_step["run"], "python3 -I .wireframe-src/scripts/wireframe_link.py")
            self.assertEqual(run_step["env"]["PR_BODY"], "${{ github.event.pull_request.body }}")
            self.assertNotIn("pull_request.body", run_step["run"])
            self.assertEqual(run_step["env"]["SPECS_READ_TOKEN"], "${{ secrets.SPECS_READ_TOKEN }}")
            self.assertEqual(run_step["env"]["BASE_REF"], "${{ github.base_ref }}")
            self.assertEqual(wf["concurrency"]["cancel-in-progress"], True)
            seen.append({k: v for k, v in run_step["env"].items() if k != "CALM_MODE"})
            self.assertFalse([s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout") and "repository" not in s.get("with", {})])
        self.assertEqual(seen[0], seen[1])

    def test_the_standalone_is_warn_by_literal_on_the_light_runner_and_the_right_events(self):
        wf, job = self.job("ux-wireframe-link.yml")
        self.assertEqual(job["steps"][1]["env"]["CALM_MODE"], "warn")
        self.assertEqual(job["runs-on"], "orbit-ci-light")
        self.assertEqual(wf[True]["pull_request"]["types"], ["opened", "edited", "synchronize", "reopened"])
        self.assertNotIn("inputs", wf[True].get("workflow_dispatch", {}))

    def test_the_reusable_defaults_to_warn_and_the_light_runner_and_the_token_is_optional(self):
        wf, _ = self.job("reusable-ux-wireframe-link.yml")
        i = wf[True]["workflow_call"]
        self.assertEqual((i["inputs"]["calm_mode"]["default"], i["inputs"]["runner"]["default"]), ("warn", "orbit-ci-light"))
        self.assertIs(i["secrets"]["SPECS_READ_TOKEN"]["required"], False)


if __name__ == "__main__":
    unittest.main(verbosity=1)
