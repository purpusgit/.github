#!/usr/bin/env python3
"""Self-test for scripts/ux_layout.py: one failing and one passing case per rule, the layout-ok escape,
added-lines-only scoping, and the gate end to end on a throwaway git repo. No network.
Run: python3 scripts/test_ux_layout.py"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ux_layout as U  # noqa: E402


def rules(path, text, module="pkg_orbit_japa", added=None):
    return [r for _, r, _ in U.scan_text(path, text, added, module)]


class Rules(unittest.TestCase):
    # ── W2 ──
    def test_w2_fails_without_kind(self):
        self.assertEqual(rules("lib/a.dart", "w() => PageContentWidth(child: Text('x'));\n"), ["W2"])

    def test_w2_passes_with_kind_even_when_a_child_has_its_own_kind(self):
        t = "w() => PageContentWidth(kind: ContentWidth.content, child: Foo(kind: 1));\n"
        self.assertEqual(rules("lib/a.dart", t), [])

    def test_w2_ignores_a_child_kind_when_the_call_has_none(self):
        self.assertEqual(rules("lib/a.dart", "w() => PageContentWidth(child: Foo(kind: 1));\n"), ["W2"])

    def test_w2_multiline_call_reports_the_first_line(self):
        t = "w() => PageContentWidth(\n  child: Text('x'),\n);\n"
        self.assertEqual(U.scan_text("lib/a.dart", t)[0][:2], (1, "W2"))

    def test_w2_ignores_the_constructor_declaration(self):
        t = "  const PageContentWidth({\n    super.key,\n    required this.kind,\n    required this.child,\n  });\n"
        self.assertEqual(rules("lib/page_content_width.dart", t), [])

    # ── W3 ──
    def test_w3_fails_on_a_hard_coded_gutter(self):
        t = "w() => PageContentWidth(kind: ContentWidth.content,\n  padding: EdgeInsets.symmetric(horizontal: 24),\n  child: x);\n"
        self.assertEqual(U.scan_text("lib/a.dart", t)[0][:2], (2, "W3"))

    def test_w3_passes_zero_vertical_only_and_computed(self):
        for pad in ("EdgeInsets.zero", "EdgeInsets.symmetric(vertical: 12)", "EdgeInsets.only(top: 8, bottom: 8)", "gutterOf(context)"):
            t = f"w() => PageContentWidth(kind: ContentWidth.content, padding: {pad}, child: x);\n"
            self.assertEqual(rules("lib/a.dart", t), [], pad)

    def test_w3_fails_all_and_scaled(self):
        for pad in ("EdgeInsets.all(24)", "EdgeInsets.only(left: 8)", "EdgeInsets.symmetric(horizontal: 24.w)"):
            t = f"w() => PageContentWidth(kind: ContentWidth.content, padding: {pad}, child: x);\n"
            self.assertEqual(rules("lib/a.dart", t), ["W3"], pad)

    # ── W1 ──
    SCREEN = "w() => Container(constraints: BoxConstraints(maxWidth: 600), child: x);\n"

    def test_w1_fails_in_a_screen_file(self):
        self.assertEqual(rules("lib/screens/home.dart", self.SCREEN), ["W1"])
        self.assertEqual(rules("lib/ui/home_screen.dart", self.SCREEN), ["W1"])

    def test_w1_fails_on_a_constant_cap(self):
        self.assertEqual(rules("lib/screens/a.dart", "w() => BoxConstraints(maxWidth: _contentMaxWidth);\n"), ["W1"])
        self.assertEqual(rules("lib/screens/a.dart", "w() => BoxConstraints(maxWidth: kMaxWidth);\n"), ["W1"])

    def test_w1_passes_outside_screens_and_for_local_sizing(self):
        self.assertEqual(rules("lib/widgets/card.dart", self.SCREEN), [])
        for v in ("double.infinity", "box.maxWidth * 0.6", "maxWidth", "160.w", "240"):  # parent-relative, local var, small label caps
            self.assertEqual(rules("lib/screens/a.dart", f"w() => BoxConstraints(maxWidth: {v});\n"), [], v)
        self.assertEqual(rules("lib/screens/a.dart", "w() => BoxConstraints(minHeight: 40);\n"), [])

    # ── W10 ──
    FIELD = "final d = InputDecoration({}: OutlineInputBorder(borderSide: BorderSide(color: s.outlineVariant)));\n"

    def test_w10_fails_on_a_resting_or_focused_field_border(self):
        for label in ("enabledBorder", "focusedBorder", "border"):
            self.assertEqual(rules("lib/a.dart", self.FIELD.format(label)), ["W10"], label)

    def test_w10_passes_on_disabled_and_error_borders(self):
        for label in ("disabledBorder", "errorBorder", "focusedErrorBorder"):
            self.assertEqual(rules("lib/a.dart", self.FIELD.format(label)), [], label)

    def test_w10_ignores_border_when_enabledBorder_sets_the_resting_one(self):
        t = ("final d = InputDecoration(border: OutlineInputBorder(borderSide: BorderSide(color: s.outlineVariant)),\n"
             "  enabledBorder: OutlineInputBorder(borderSide: BorderSide(color: s.outline)));\n")
        self.assertEqual(rules("lib/a.dart", t), [])

    def test_w10_passes_on_a_card_border_and_a_divider(self):
        t = "final d = BoxDecoration(border: Border.all(color: s.outlineVariant));\nfinal e = Divider(color: s.outlineVariant);\n"
        self.assertEqual(rules("lib/a.dart", t), [])

    # ── W11 ──
    def test_w11_fails_in_a_web_file(self):
        self.assertEqual(rules("lib/x_web.dart", "final p = EdgeInsets.all(16.w);\n"), ["W11"])
        self.assertEqual(rules("lib/x_web.dart", "final f = TextStyle(fontSize: 12.sp);\n"), ["W11"])

    def test_w11_passes_in_other_files_and_for_raw_numbers(self):
        self.assertEqual(rules("lib/x_mobile.dart", "final p = EdgeInsets.all(16.w);\n"), [])
        self.assertEqual(rules("lib/x_web.dart", "final p = EdgeInsets.all(16); // not 16.w\n"), [])

    # ── W12 ──
    def test_w12_fails_outside_binder_and_passes_inside(self):
        t = "void main() { ScreenUtil.init(ctx); }\nw() => ScreenUtilInit(child: x);\n"
        self.assertEqual(rules("lib/main.dart", t, module="pkg_orbit_japa"), ["W12", "W12"])
        self.assertEqual(rules("lib/main.dart", t, module="pkg_orbit_binder"), [])
        self.assertEqual(rules("lib/main.dart", t, module="orbit_binder purpusgit/pkg_orbit_binder"), [])

    def test_w12_passes_in_test_support(self):
        self.assertEqual(rules("lib/test_support/harness.dart", "w() => ScreenUtilInit(child: x);\n", module="orbit_client_core"), [])

    # ── DBG ──
    def test_dbg_fails_on_an_unguarded_bug_icon_and_dev_route(self):
        t = "Widget build(c) {\n  return IconButton(icon: Icon(Icons.bug_report), onPressed: go);\n}\n"
        self.assertEqual(rules("lib/a.dart", t), ["DBG"])
        self.assertEqual(rules("lib/r.dart", "final r = GetPage(name: '/playground', page: P.new);\n"), ["DBG"])
        self.assertEqual(rules("lib/r.dart", "final r = DevToolsScreen();\n"), ["DBG"])

    def test_dbg_passes_under_kdebugmode(self):
        t = "Widget build(c) {\n  return Row(children: [\n    if (kDebugMode) IconButton(icon: Icon(Icons.bug_report)),\n  ]);\n}\n"
        self.assertEqual(rules("lib/a.dart", t), [])
        t = "Widget build(c) {\n  if (!kDebugMode) return const SizedBox();\n  return Icon(Icons.bug_report);\n}\n"
        self.assertEqual(rules("lib/a.dart", t), [])

    def test_dbg_passes_a_dev_flavor_guard_a_report_bug_row_and_a_dev_tool_own_files(self):
        guard = "Widget b(c) {\n  if (FlavorConfig().flavor == AppFlavor.japa_development) return Icon(Icons.bug_report);\n  return x;\n}\n"
        self.assertEqual(rules("lib/a.dart", guard), [])
        row = "w() => Row(children: [Icon(Icons.bug_report_outlined), Text('Report a problem')]);\n"
        self.assertEqual(rules("lib/a.dart", row), [])
        self.assertEqual(rules("lib/screens/dev/picker_dev.screen.dart", "w() => Icon(Icons.bug_report);\nclass PickerDevScreen {}\n"), [])

    def test_dbg_fails_on_a_dev_screen_route_registration_but_not_its_class_declaration(self):
        self.assertEqual(rules("lib/routes.dart", "final p = GetPage(name: '/x', page: () => TaxonomyPickerDevTestScreen());\n"), ["DBG"])
        self.assertEqual(rules("lib/s.dart", "class TaxonomyPickerDevTestScreen extends StatefulWidget {}\n"), [])

    # ── the escape ──
    def test_layout_ok_on_the_line_or_the_line_above(self):
        call = "w() => PageContentWidth(child: x);"
        self.assertEqual(rules("lib/a.dart", call + " // layout-ok: legacy shell, tracked in specs#1\n"), [])
        self.assertEqual(rules("lib/a.dart", "// layout-ok: legacy shell\n" + call + "\n"), [])

    def test_layout_ok_needs_a_reason_and_a_comment_line_above(self):
        call = "w() => PageContentWidth(child: x);"
        self.assertEqual(rules("lib/a.dart", call + " // layout-ok:\n"), ["W2"])
        self.assertEqual(rules("lib/a.dart", call + " // layout-ok:    \n"), ["W2"])
        self.assertEqual(rules("lib/a.dart", "final z = 1; // layout-ok: x\n" + call + "\n"), ["W2"])

    def test_layout_ok_on_the_calls_first_line_covers_a_later_padding_line(self):
        t = ("w() => PageContentWidth( // layout-ok: dialog owns no gutter\n  kind: ContentWidth.auth,\n"
             "  padding: EdgeInsets.all(24),\n  child: x);\n")
        self.assertEqual(rules("lib/a.dart", t), [])

    # ── scoping ──
    def test_only_added_lines_count(self):
        t = "w() => PageContentWidth(child: a);\nv() => PageContentWidth(child: b);\n"
        self.assertEqual([n for n, _, _ in U.scan_text("lib/a.dart", t, {2})], [2])
        self.assertEqual(U.scan_text("lib/a.dart", t, set()), [])

    def test_comments_and_strings_are_not_code(self):
        t = "// PageContentWidth(child: x)\nfinal s = 'PageContentWidth(child: x)';\n/* ScreenUtil.init( */\n"
        self.assertEqual(rules("lib/a_web.dart", t), [])


class Workflow(unittest.TestCase):
    """The workflow diffs against the PR's own base: a PR into master or sandbox must not be judged against cwb."""

    def test_base_defaults_to_the_prs_base_branch(self):
        import yaml
        wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "reusable-ux-layout.yml")))
        self.assertEqual(wf[True]["workflow_call"]["inputs"]["base_ref"]["default"], "")
        env = next(st for st in wf["jobs"]["ux-layout"]["steps"] if st.get("env"))["env"]["BASE"]
        self.assertEqual(env, "${{ inputs.base_ref || github.base_ref || 'cwb' }}")


class Standalone(unittest.TestCase):
    """An org ruleset can only require a standalone `on: pull_request` workflow with no inputs. ux-required.yml is that file;
    it must run the SAME script as the reusable, fetched the same way, so the two cannot drift."""

    @staticmethod
    def wf(name):
        import yaml
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", name)) as fh:
            return yaml.safe_load(fh)

    @staticmethod
    def steps(wf, job):
        return wf["jobs"][job]["steps"]

    def test_it_is_a_standalone_pull_request_workflow_with_no_inputs(self):
        wf = self.wf("ux-required.yml")
        self.assertEqual(set(wf[True]), {"pull_request"})
        # `reopened` matters: without it a closed-and-reopened PR never reports the required check and waits forever
        self.assertEqual(wf[True]["pull_request"]["types"], ["opened", "synchronize", "edited", "reopened"])
        self.assertIn("ux-layout", wf["jobs"])  # the job name is the required context

    def test_it_runs_the_same_script_fetched_the_same_way_as_the_reusable(self):
        a, b = self.steps(self.wf("reusable-ux-layout.yml"), "ux-layout"), self.steps(self.wf("ux-required.yml"), "ux-layout")
        fetch = lambda ss: next(s["with"] for s in ss if s.get("with", {}).get("repository") == "purpusgit/.github")
        run = lambda ss: next(s["run"] for s in ss if "ux_layout.py" in s.get("run", ""))
        self.assertEqual(fetch(a), fetch(b))
        self.assertEqual(run(a), run(b))
        self.assertEqual(fetch(b)["ref"], "main")  # a PR cannot change the rules that judge it

    def test_it_takes_its_base_from_the_prs_base_branch_and_reads_no_input(self):
        b = self.steps(self.wf("ux-required.yml"), "ux-layout")
        base = next(s["env"]["BASE"] for s in b if s.get("env", {}).get("BASE"))
        self.assertEqual(base, "${{ github.base_ref || 'cwb' }}")
        self.assertNotIn("inputs.", open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "ux-required.yml")).read().split("jobs:")[1])

    def test_it_no_ops_in_a_repo_without_pubspec_yaml(self):
        b = self.steps(self.wf("ux-required.yml"), "ux-layout")
        probe = next(s for s in b if s.get("id") == "flutter")
        self.assertIn("pubspec.yaml", probe["run"])
        for s in b:
            if "ux_layout.py" in s.get("run", "") or s.get("with", {}).get("repository") == "purpusgit/.github":
                self.assertIn("steps.flutter.outputs.yes == 'true'", s.get("if", ""), s.get("name"))


def run(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(base_files, head_files, pubspec="name: pkg_orbit_japa\n"):
    root = tempfile.mkdtemp(prefix="uxlayout-")
    run("git", "init", "-q", "-b", "base", cwd=root)
    for step, files in (("base", base_files), ("head", head_files)):
        if step == "head":
            run("git", "checkout", "-qb", "head", cwd=root)
        for rel, body in {**{"pubspec.yaml": pubspec}, **files}.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(body)
        run("git", "add", "-A", cwd=root)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", step, cwd=root)
    return root


class Gate(unittest.TestCase):
    def gate(self, root, base="base"):
        old = os.getcwd()
        os.chdir(root)
        try:
            return U.gate(base)
        finally:
            os.chdir(old)

    OLD = "lib/old.dart"
    OLD_BODY = "w() => PageContentWidth(child: x);\n"  # a pre-existing blunder: backlog, not this PR

    def test_pre_existing_blunders_pass_and_a_new_one_fails(self):
        clean = repo({self.OLD: self.OLD_BODY}, {self.OLD: self.OLD_BODY, "lib/new.dart": "w() => Text('x');\n"})
        self.assertEqual(self.gate(clean), 0)
        bad = repo({self.OLD: self.OLD_BODY}, {self.OLD: self.OLD_BODY, "lib/new.dart": "w() => PageContentWidth(child: x);\n"})
        self.assertEqual(self.gate(bad), 1)

    def test_a_line_added_to_an_old_call_counts_but_an_untouched_call_does_not(self):
        root = repo({self.OLD: "a() => 1;\n" + self.OLD_BODY}, {self.OLD: "a() => 2;\n" + self.OLD_BODY})
        self.assertEqual(self.gate(root), 0)

    def test_the_escape_passes_the_gate(self):
        root = repo({}, {"lib/new.dart": "w() => PageContentWidth(child: x); // layout-ok: kiosk\n"})
        self.assertEqual(self.gate(root), 0)

    def test_screenutil_init_in_binder_passes_elsewhere_fails(self):
        files = {"lib/main.dart": "void main() { ScreenUtil.init(c); }\n"}
        self.assertEqual(self.gate(repo({}, files, "name: pkg_orbit_binder\n")), 0)
        self.assertEqual(self.gate(repo({}, files, "name: pkg_orbit_japa\n")), 1)

    def test_generated_and_non_lib_files_are_ignored(self):
        root = repo({}, {"lib/a.g.dart": "w() => PageContentWidth(child: x);\n", "test/a_test.dart": "w() => PageContentWidth(child: x);\n"})
        self.assertEqual(self.gate(root), 0)

    def test_an_unresolvable_base_fails_closed(self):
        self.assertEqual(self.gate(repo({}, {"lib/a.dart": "x;\n"}), "no-such-ref"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
