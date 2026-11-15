#!/usr/bin/env python3
"""Self-test for scripts/calm_static.py: the static rows of CALM-1 spec section 7 (rev 5.1), on throwaway git repos. No network.
Run: python3 scripts/test_calm_static.py"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import calm_static as C  # noqa: E402

SCAFFOLD = "import 'package:flutter/material.dart';\nclass %s extends StatelessWidget {\n  @override\n  Widget build(BuildContext context) {\n    return Scaffold(body: Text('x'));\n  }\n}\n"


def manifest(*rows, pkg="my_app"):
    out = "package: %s\ncap_gate: false\nscreens:\n" % pkg
    for name, file_, build in rows:
        out += "  %s:\n    file: %s\n    cap: reading\n    web_branch: false\n    tested: true\n    build: \"%s\"\n" % (name, file_, build)
    return out


MANIFEST = "test/UI/web_visual/manifest.yaml"
ROW_FOO = ("FooScreen", "lib/ui/foo_screen.dart", "const FooScreen()")


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(base_files, head_files, pubspec="name: my_app\n"):
    root = tempfile.mkdtemp(prefix="calm-")
    run(["git", "init", "-q", "-b", "base"], root)
    for step, files in (("base", base_files), ("head", head_files)):
        if step == "head":
            run(["git", "checkout", "-qb", "head"], root)
        for rel, body in {**{"pubspec.yaml": pubspec}, **files}.items():
            if body is None:
                if os.path.exists(os.path.join(root, rel)):
                    os.remove(os.path.join(root, rel))
                continue
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(body)
        run(["git", "add", "-A"], root)
        run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", step, "--allow-empty"], root)
    return root


def analyze(root, body=""):
    cwd = os.getcwd()
    os.chdir(root)
    try:
        bm, hm, changed, read, libs, pkg = C.gather("base", root)
        return C.analyze(bm, hm, changed, read, libs, body, pkg)
    finally:
        os.chdir(cwd)


def main(root, mode="warn", body="", emit=False, base="base"):
    cwd, old = os.getcwd(), dict(os.environ)
    os.chdir(root)
    os.environ.update(BASE_REF=base, CALM_MODE=mode, PR_BODY=body)
    os.environ.pop("GITHUB_STEP_SUMMARY", None)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = C.main(["--emit-enforce"] if emit else [])
        return code, out.getvalue(), err.getvalue()
    finally:
        os.chdir(cwd); os.environ.clear(); os.environ.update(old)


BASE = {MANIFEST: manifest(ROW_FOO), "lib/ui/foo_screen.dart": SCAFFOLD % "FooScreen"}


class NoRow(unittest.TestCase):
    """Spec 7: a changed lib/**/foo_screen.dart whose class builds a Scaffold and has no manifest row fails the static step."""

    def test_a_changed_screen_with_no_row_is_a_finding(self):
        r = analyze(repo(BASE, {"lib/ui/bar_screen.dart": SCAFFOLD % "BarScreen"}))
        self.assertEqual([(p, c) for p, c, _ in r["no_row"]], [("lib/ui/bar_screen.dart", "BarScreen")])

    def test_warn_prints_and_passes_enforce_fails(self):
        root = repo(BASE, {"lib/ui/bar_screen.dart": SCAFFOLD % "BarScreen"})
        code, out, _ = main(root, "warn")
        self.assertEqual(code, 0)
        self.assertIn("::warning file=lib/ui/bar_screen.dart::CALM-STATIC BarScreen", out)
        self.assertIn("add this screen to the web-width manifest", out)
        code, out, _ = main(root, "enforce")
        self.assertEqual(code, 1)
        self.assertIn("::error file=lib/ui/bar_screen.dart", out)

    def test_a_screen_with_a_row_is_fine(self):
        r = analyze(repo(BASE, {"lib/ui/foo_screen.dart": SCAFFOLD % "FooScreen" + "// edit\n"}))
        self.assertEqual(r["no_row"], [])

    def test_a_renamed_file_does_not_dodge_the_rule(self):
        files = {"lib/ui/foo_screen.dart": None, "lib/ui/renamed_thing.dart": SCAFFOLD % "FooScreen"}
        r = analyze(repo(BASE, files))
        self.assertEqual([c for _, c, _ in r["no_row"]], ["FooScreen"])  # a row is keyed by file + class: the old row names the old file

    def test_a_file_that_only_calls_show_dialog_or_a_sheet_passes(self):
        src = ("class Picker extends StatelessWidget {\n  Widget build(BuildContext c) {\n    return TextButton(onPressed: () => showDialog(context: c, "
               "builder: (_) => AlertDialog(title: Text('x'))), child: Text('go'));\n  }\n}\n")
        self.assertEqual(analyze(repo(BASE, {"lib/ui/picker.dart": src}))["no_row"], [])
        src2 = "void open(c) { showModalBottomSheet(context: c, builder: (_) => Container()); }\n"
        self.assertEqual(analyze(repo(BASE, {"lib/ui/open.dart": src2}))["no_row"], [])

    def test_a_file_under_test_support_passes(self):
        self.assertEqual(analyze(repo(BASE, {"lib/test_support/harness_screen.dart": SCAFFOLD % "HarnessScreen"}))["no_row"], [])

    def test_the_sheet_body_class_is_a_screen_but_only_if_declared_in_the_file(self):
        src = ("class Opener { void go(c) { showModalBottomSheet(context: c, builder: (_) => FilterSheet()); } }\n"
               "class FilterSheet extends StatelessWidget { Widget build(c) => Column(children: []); }\n")
        self.assertEqual([(c, k) for _, c, k in analyze(repo(BASE, {"lib/ui/filters.dart": src}))["no_row"]], [("FilterSheet", "sheet")])
        src2 = "class Opener { void go(c) { showModalBottomSheet(context: c, builder: (_) => ElsewhereSheet()); } }\n"
        self.assertEqual(analyze(repo(BASE, {"lib/ui/o.dart": src2}))["no_row"], [])

    def test_each_screen_widget_counts_and_a_state_counts_for_its_widget(self):
        for w in ("Scaffold", "Dialog", "AlertDialog", "DraggableScrollableSheet"):
            src = "class S extends StatelessWidget { Widget build(c) => %s(child: x); }\n" % w
            self.assertEqual([c for _, c, _ in analyze(repo(BASE, {"lib/ui/s.dart": src}))["no_row"]], ["S"], w)
        st = ("class Foo extends StatefulWidget { State<Foo> createState() => _S(); }\n"
              "class _S extends State<Foo> { Widget build(c) { return Scaffold(); } }\n")
        self.assertEqual([c for _, c, _ in analyze(repo(BASE, {"lib/ui/foo.dart": st}))["no_row"]], ["Foo"])

    def test_a_plain_widget_a_comment_a_deleted_file_and_a_generated_file_are_not_findings(self):
        files = {"lib/ui/card.dart": "class Card2 extends StatelessWidget { Widget build(c) { return Card(child: x); } }\n",
                 "lib/ui/old.dart": "// class Old { Widget build(c) { return Scaffold(); } }\n",
                 "lib/ui/foo_screen.dart": None,
                 "lib/ui/a.g.dart": SCAFFOLD % "Gen"}
        self.assertEqual(analyze(repo({**BASE, "lib/ui/gone_screen.dart": SCAFFOLD % "GoneScreen"}, {**files, "lib/ui/gone_screen.dart": None}))["no_row"], [])

    def test_the_class_a_row_stands_for_comes_from_its_build_when_the_name_differs(self):
        m = manifest(("DialogRow", "lib/ui/dlg.dart", "WebDialogHost(child: ConfirmDialog(title: 'x'))"))
        src = "class ConfirmDialog extends StatelessWidget { Widget build(c) => AlertDialog(title: x); }\n"
        self.assertEqual(analyze(repo({MANIFEST: m, "lib/ui/dlg.dart": src}, {"lib/ui/dlg.dart": src + "// edit\n"}))["no_row"], [])


class ManifestChange(unittest.TestCase):
    LINE = "calm-manifest-change: FooScreen: build changed on purpose"

    def test_setting_an_existing_rows_build_to_sizedbox_needs_the_line(self):
        head = {MANIFEST: manifest(("FooScreen", "lib/ui/foo_screen.dart", "SizedBox()"))}
        root = repo(BASE, head)
        r = analyze(root)
        self.assertEqual([(row, what) for row, _, what in r["manifest"]], [("FooScreen", "changed (build)")])
        self.assertEqual(main(root, "enforce")[0], 1)
        self.assertEqual(analyze(root, "Notes\n" + self.LINE + "\n")["manifest"], [])
        self.assertEqual(main(root, "enforce", self.LINE)[0], 0)

    def test_removing_row_x_and_adding_row_y_with_the_same_file_and_sizedbox_needs_both_lines(self):
        m = manifest(("YScreen", "lib/ui/foo_screen.dart", "SizedBox()"))
        head = {MANIFEST: m, "lib/ui/foo_screen.dart": SCAFFOLD % "YScreen"}
        r = analyze(repo(BASE, head))
        self.assertEqual(sorted((row, what) for row, _, what in r["manifest"]), [("FooScreen", "removed"), ("YScreen", "added")])
        both = "calm-manifest-change: FooScreen: replaced by YScreen\ncalm-manifest-change: YScreen: the new frame\n"
        self.assertEqual(analyze(repo(BASE, head), both)["manifest"], [])
        only_x = "calm-manifest-change: FooScreen: replaced by YScreen\n"
        self.assertEqual([row for row, _, _ in analyze(repo(BASE, head), only_x)["manifest"]], ["YScreen"])

    def test_adding_any_row_needs_the_line_even_a_flavour_home(self):
        m = manifest(ROW_FOO, ("HomeScreen", "lib/ui/home_screen.dart", "const HomeScreen()"))
        head = {MANIFEST: m, "lib/ui/home_screen.dart": SCAFFOLD % "HomeScreen"}
        r = analyze(repo(BASE, head))
        self.assertEqual([(row, what) for row, _, what in r["manifest"]], [("HomeScreen", "added")])
        self.assertEqual(analyze(repo(BASE, head), "calm-manifest-change: HomeScreen: new flavour home\n")["manifest"], [])

    def test_a_missing_or_placeholder_reason_does_not_count(self):
        head = {MANIFEST: manifest(("FooScreen", "lib/ui/foo_screen.dart", "SizedBox()"))}
        root = repo(BASE, head)
        for body in ("calm-manifest-change: FooScreen:", "calm-manifest-change: FooScreen: <why>", "calm-manifest-change: FooScreen: x",
                     "calm-manifest-change: OtherRow: a real reason", "I changed the manifest on purpose"):
            self.assertEqual(len(analyze(root, body)["manifest"]), 1, body)

    def test_a_row_that_only_changes_its_row_name_or_cap_is_not_a_change(self):
        m = manifest(("RenamedRow", "lib/ui/foo_screen.dart", "const FooScreen()")).replace("cap: reading", "cap: content")
        self.assertEqual(analyze(repo(BASE, {MANIFEST: m}))["manifest"], [])

    def test_a_flavour_flag_change_needs_the_line(self):
        m = manifest(ROW_FOO).replace("    build:", "    flavour_home: true\n    build:")
        self.assertEqual([what for _, _, what in analyze(repo(BASE, {MANIFEST: m}))["manifest"]], ["changed (flavour_home)"])


class ImportClosure(unittest.TestCase):
    TILE = "lib/ui/item_card_tile.dart"

    def files(self, screen_imports):
        return {MANIFEST: manifest(ROW_FOO, ("Other", "lib/ui/other_screen.dart", "const Other()")),
                "lib/ui/foo_screen.dart": screen_imports + (SCAFFOLD % "FooScreen"),
                "lib/ui/other_screen.dart": SCAFFOLD % "Other",
                self.TILE: "class ItemCardTile {}\n"}

    def enforce(self, base, head):
        return analyze(repo(base, head))["enforce"]

    def test_a_changed_file_a_screen_imports_makes_that_screen_new_only(self):
        for imp in ("import 'package:my_app/ui/item_card_tile.dart';\n", "import 'item_card_tile.dart';\n", "import '../ui/item_card_tile.dart';\n"):
            base = self.files(imp)
            head = {self.TILE: "class ItemCardTile { final x = 1; }\n"}
            self.assertEqual(self.enforce(base, head), ["FooScreen:new"], imp)

    def test_a_screens_own_changed_file_is_full_and_unrelated_screens_are_left_alone(self):
        base = self.files("import 'item_card_tile.dart';\n")
        self.assertEqual(self.enforce(base, {"lib/ui/foo_screen.dart": base["lib/ui/foo_screen.dart"] + "// edit\n"}), ["FooScreen"])
        self.assertEqual(self.enforce(base, {"lib/ui/other_screen.dart": base["lib/ui/other_screen.dart"] + "// edit\n"}), ["Other"])

    def test_the_closure_is_transitive_and_follows_export_and_part(self):
        base = {MANIFEST: manifest(ROW_FOO), "lib/ui/foo_screen.dart": "import 'mid.dart';\n" + SCAFFOLD % "FooScreen",
                "lib/ui/mid.dart": "export 'leaf.dart';\n", "lib/ui/leaf.dart": "class Leaf {}\n"}
        self.assertEqual(self.enforce(base, {"lib/ui/leaf.dart": "class Leaf { int x = 1; }\n"}), ["FooScreen:new"])
        base2 = {MANIFEST: manifest(ROW_FOO), "lib/ui/foo_screen.dart": "part 'foo_parts.dart';\n" + SCAFFOLD % "FooScreen",
                 "lib/ui/foo_parts.dart": "part of 'foo_screen.dart';\n"}
        self.assertEqual(self.enforce(base2, {"lib/ui/foo_parts.dart": "part of 'foo_screen.dart';\n// edit\n"}), ["FooScreen:new"])

    def test_another_package_and_dart_imports_are_not_in_the_closure(self):
        base = self.files("import 'package:other_pkg/ui/item_card_tile.dart';\nimport 'dart:async';\n")
        self.assertEqual(self.enforce(base, {self.TILE: "class ItemCardTile { final x = 1; }\n"}), [])

    def test_emit_enforce_prints_exactly_one_line_and_nothing_else(self):
        base = self.files("import 'item_card_tile.dart';\n")
        root = repo(base, {self.TILE: "class ItemCardTile { final x = 1; }\n", "lib/ui/other_screen.dart": base["lib/ui/other_screen.dart"] + "// e\n"})
        code, out, err = main(root, emit=True)
        self.assertEqual((code, out), (0, "CALM_ENFORCE=FooScreen:new,Other\n"))
        code, out, _ = main(repo(base, {"lib/unrelated.dart": "class U {}\n"}), emit=True)
        self.assertEqual((code, out), (0, "CALM_ENFORCE=\n"))


class Edges(unittest.TestCase):
    def test_no_manifest_anywhere_is_a_notice_and_a_pass_even_in_enforce(self):
        root = repo({}, {"lib/ui/bar_screen.dart": SCAFFOLD % "BarScreen"})
        for mode in ("warn", "enforce"):
            code, out, _ = main(root, mode)
            self.assertEqual(code, 0)
            self.assertIn("no web-width manifest in this repo", out)

    def test_a_manifest_added_by_the_pr_makes_every_row_an_added_row(self):
        r = analyze(repo({"lib/ui/foo_screen.dart": SCAFFOLD % "FooScreen"}, {MANIFEST: manifest(ROW_FOO)}))
        self.assertEqual([what for _, _, what in r["manifest"]], ["added"])

    def test_an_unreadable_base_fails_closed_in_every_mode(self):
        root = repo(BASE, {"lib/x.dart": "class X {}\n"})
        for mode in ("warn", "enforce"):
            self.assertEqual(main(root, mode, base="no-such-ref")[0], 2)
        self.assertEqual(main(root, emit=True, base="no-such-ref")[0], 2)

    def test_the_manifest_reader_copes_with_quotes_colons_comments_and_blocks(self):
        t = ('package: my_app\nscreens:\n  A:\n    file: lib/a.dart  # the file\n    build: "Foo(a: 1, b: \'x # y\')"\n    states:\n      - one\n      - two\n'
             '  B:\n    file: lib/b.dart\n    build: |\n      Bar(\n        c: 2)\n    flavour_home: true\n')
        pkg, rows = C.parse_manifest(t)
        self.assertEqual((pkg, sorted(rows)), ("my_app", ["A", "B"]))
        self.assertEqual(rows["A"], {"file": "lib/a.dart", "build": "Foo(a: 1, b: 'x # y')", "states": ""})
        self.assertEqual(rows["B"]["build"], "Bar( c: 2)")
        self.assertEqual(rows["B"]["flavour_home"], "true")


class Workflow(unittest.TestCase):
    @staticmethod
    def wf():
        import yaml
        with open(os.path.join(HERE, "..", ".github", "workflows", "reusable-consolidated-gates.yml")) as fh:
            return yaml.safe_load(fh)

    def steps(self):
        job = next(iter(self.wf()["jobs"].values()))
        return {s.get("id"): s for s in job["steps"] if s.get("id")}

    def test_it_is_opt_in_and_warn_by_default(self):
        i = self.wf()[True]["workflow_call"]["inputs"]
        self.assertIs(i["run_calm_static"]["default"], False)
        self.assertEqual(i["calm_mode"]["default"], "warn")

    def test_the_script_comes_from_main_and_is_run_isolated_with_the_body_in_the_environment(self):
        s = self.steps()
        fetch, step = s["calm_src"]["with"], s["calm"]
        self.assertEqual((fetch["repository"], fetch["ref"], fetch["sparse-checkout"].strip()), ("purpusgit/.github", "main", "scripts/calm_static.py"))
        self.assertIn("python3 -I .calm-src/scripts/calm_static.py", step["run"])
        self.assertEqual(step["env"]["PR_BODY"], "${{ github.event.pull_request.body }}")
        self.assertNotIn("pull_request.body", step["run"])  # never inlined into the script text
        self.assertEqual(step["env"]["CALM_MODE"], "${{ inputs.calm_mode }}")
        self.assertEqual(s["calm_src"]["if"], "${{ inputs.run_calm_static }}")
        self.assertEqual(step["if"], "${{ inputs.run_calm_static }}")

    def test_both_steps_have_a_verdict_row(self):
        import re
        text = open(os.path.join(HERE, "..", ".github", "workflows", "reusable-consolidated-gates.yml")).read()
        for sid in ("calm_src", "calm"):
            self.assertRegex(text, r"steps\.%s\.outcome" % sid)
            self.assertEqual(len(re.findall(r"^\s+CALM-1 .*steps\.%s\.outcome" % sid, text, re.M)), 1, sid)


if __name__ == "__main__":
    unittest.main(verbosity=1)
