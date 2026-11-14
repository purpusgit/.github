#!/usr/bin/env python3
"""Self-test for scripts/ux_one_template.py: a failing and a passing case per pattern, the allowlist (it can only shrink,
a PR cannot extend it), client_core's exemption, added-lines scoping, and the gate end to end on throwaway git repos.
No network. Run: python3 scripts/test_ux_one_template.py"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ux_one_template as U  # noqa: E402

# The allowlist may only SHRINK. Adding a line to it must fail this test; to remove a line, delete it from the file
# (the subset check below still holds). Do not add to this set: adding an app frame needs UX-01, not a test edit.
CEILING = frozenset({
    ("pkg_orbit_binder", "lib/src/screens/shell/orbit_shell.screen.dart", "OrbitShellScreen"),
    ("pkg_orbit_binder", "lib/src/screens/shell/orbit_shell.screen.dart", "NavigationBar"),
    ("pkg_orbit_binder", "lib/src/screens/shell/orbit_shell.screen.dart", "NavigationRail"),
    ("pkg_goalcaller_voicereach", "lib/screens/shell/voicereach-shell.screen.dart", "VoiceReachShellScreen"),
    ("pkg_orbit_binder", "lib/src/screens/shell/orbit_shell.screen.dart", "OrbitShellScope"),
    ("pkg_orbit_binder", "lib/src/screens/dashboard/japa_shell.screen.dart", "JapaShellScreen"),
    ("pkg_orbit_binder", "lib/src/screens/shell/shell_home.tab.dart", "ShellHomeTab"),
    ("pkg_orbit_binder", "lib/src/screens/shell/shell_home.tab.dart", "ShellHomeSkeleton"),
    ("pkg_orbit_binder", "lib/src/screens/shell/shell_persona.widgets.dart", "ShellPersonaPicker"),
    ("pkg_orbit_binder", "lib/src/screens/shell/shell_persona.widgets.dart", "ShellPersonaChip"),
    ("pkg_orbit_binder", "lib/src/screens/shell/shell_persona.widgets.dart", "ShellSuggestedCards"),
    ("main_org_orbit", "lib/demo/shell_demo.dart", "ShellDemoSwitcher"),
    ("main_org_orbit", "lib/demo/shell_demo.dart", "ShellDemoComingSoonCard"),
    ("main_org_orbit", "lib/demo/shell_demo.dart", "ShellDemoComingSoonTab"),
    ("main_org_orbit", "lib/demo/shell_demo.dart", "ShellDemoComingSoonScreen"),
    ("pkg_goalcaller_voicereach", "lib/screens/shell/voicereach-shell.screen.dart", "NavigationRail"),
    ("pkg_goalcaller_voicereach", "lib/screens/shell/voicereach-shell.screen.dart", "NavigationBar"),
    ("main_org_orbit", "lib/modules/main_orbit_module/dashboard.screen.dart", "BottomNavigationBar"),
    ("main_org_orbit", "lib/modules/main_orbit_module/main_orbit_module.dart", "BottomNavigationBar"),
})


def hits(text, path="lib/a.dart", repos=("pkg_orbit_japa",), allow=frozenset(), added=None):
    return [(k, n) for _, k, n in U.scan_text(path, text, added, repos, allow)]


class Patterns(unittest.TestCase):
    def test_frame_widgets_fail(self):
        for w in ("NavigationRail", "NavigationBar", "BottomNavigationBar"):
            self.assertEqual(hits(f"w() => Scaffold(body: {w}(destinations: d));\n"), [("widget", w)], w)

    def test_near_misses_pass(self):
        for t in ("w() => NavigationRailDestination(icon: i, label: l);\n", "w() => NavigationBarTheme(data: d, child: c);\n",
                  "w() => BottomNavigationBarItem(icon: i);\n", "final n = MyNavigationBar();\n",
                  "// NavigationRail( is the shell's job\n", "final s = 'NavigationBar(';\n", "/* BottomNavigationBar( */\n"):
            self.assertEqual(hits(t), [], t)

    def test_widget_classes_named_shell_or_sidemenu_fail(self):
        for t, name in (("class FooShell extends StatelessWidget {\n}\n", "FooShell"),
                        ("class SideMenuTile extends StatefulWidget {\n}\n", "SideMenuTile"),
                        ("final class MyShellScreen extends ConsumerWidget {\n}\n", "MyShellScreen"),
                        ("class ShellPanel extends GetView<C> {\n}\n", "ShellPanel"),
                        ("class OrbitShellScope extends InheritedWidget {\n}\n", "OrbitShellScope")):
            self.assertEqual(hits(t), [("class", name)], t)

    def test_non_widget_shell_classes_and_state_companions_pass(self):
        for t in ("class FooShellContributions {\n}\n", "class FooShellContributions implements ShellContributions {\n}\n",
                  "class _FooShellState extends State<FooShell> {\n}\n", "class OrbitShellController extends GetxController {\n}\n",
                  "class ShellHomeRedirect extends GetMiddleware {\n}\n", "class HomeScreen extends StatelessWidget {\n}\n",
                  "// class FooShell extends StatelessWidget {\n", "final s = 'class FooShell extends StatelessWidget {';\n"):
            self.assertEqual(hits(t), [], t)

    def test_only_added_lines_count(self):
        t = "w() => NavigationBar(a: 1);\nv() => NavigationRail(b: 2);\n"
        self.assertEqual([n for _, _, n in U.scan_text("lib/a.dart", t, {2}, ("r",), frozenset())], ["NavigationRail"])
        self.assertEqual(hits(t, added=set()), [])

    def test_client_core_is_exempt_by_repo_or_pubspec_name(self):
        t = "w() => NavigationRail(a: 1);\nclass AppShell extends StatelessWidget {\n}\n"
        self.assertEqual(hits(t, repos=("pkg_orbit_client_core",)), [])
        self.assertEqual(hits(t, repos=("orbit_client_core", "")), [])
        self.assertEqual(len(hits(t, repos=("pkg_orbit_japa",))), 2)


class Allowlist(unittest.TestCase):
    P = "lib/src/screens/shell/orbit_shell.screen.dart"
    ALLOW = frozenset({("pkg_orbit_binder", P, "NavigationBar")})

    def test_an_allowlisted_pair_passes_in_its_repo_and_path_only(self):
        t = "w() => NavigationBar(a: 1);\n"
        self.assertEqual(hits(t, self.P, ("pkg_orbit_binder",), self.ALLOW), [])
        self.assertEqual(hits(t, "lib/other.dart", ("pkg_orbit_binder",), self.ALLOW), [("widget", "NavigationBar")])
        self.assertEqual(hits(t, self.P, ("pkg_orbit_japa",), self.ALLOW), [("widget", "NavigationBar")])
        self.assertEqual(hits("w() => NavigationRail(a: 1);\n", self.P, ("pkg_orbit_binder",), self.ALLOW), [("widget", "NavigationRail")])

    def test_the_shipped_allowlist_is_well_formed_and_every_path_is_lib(self):
        allow = U.load_allowlist()
        self.assertTrue(allow)
        for repo, path, name in allow:
            self.assertTrue(path.startswith("lib/") and path.endswith(".dart"), path)
            self.assertRegex(name, r"^\w+$")

    def test_the_allowlist_can_only_shrink(self):
        extra = U.load_allowlist() - CEILING
        self.assertEqual(extra, set(), "an entry was ADDED to the allowlist: " + repr(sorted(extra)))

    def test_the_allowlist_has_no_entry_beyond_origin_main(self):
        """Where origin/main is available (a PR run), the file may only lose lines against it."""
        root = os.path.dirname(HERE)
        try:
            base = subprocess.run(["git", "show", "origin/main:scripts/ux_one_template_allowlist.txt"], cwd=root,
                                  check=True, capture_output=True, text=True).stdout
        except (subprocess.CalledProcessError, OSError):
            self.skipTest("origin/main has no allowlist yet (first PR) or is not fetched")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(base)
        try:
            extra = U.load_allowlist() - U.load_allowlist(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(extra, set(), "an entry was ADDED against origin/main: " + repr(sorted(extra)))

    def test_a_malformed_allowlist_line_is_an_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("pkg_x lib/a.dart\n")
        try:
            with self.assertRaises(ValueError):
                U.load_allowlist(fh.name)
        finally:
            os.unlink(fh.name)

    def test_the_allowlist_sits_next_to_the_script_not_in_the_checked_repo(self):
        self.assertEqual(os.path.dirname(U.ALLOWLIST), HERE)


class Standalone(unittest.TestCase):
    """The org-ruleset target (ux-required.yml, job `ux-one-template`) runs the SAME script and allowlist as the reusable."""

    @staticmethod
    def wf(name):
        import yaml
        with open(os.path.join(HERE, "..", ".github", "workflows", name)) as fh:
            return yaml.safe_load(fh)

    def test_the_job_exists_under_its_required_context_name(self):
        wf = self.wf("ux-required.yml")
        self.assertEqual(set(wf[True]), {"pull_request"})
        self.assertIn("ux-one-template", wf["jobs"])

    def test_it_runs_the_same_script_and_fetches_the_same_files_as_the_reusable(self):
        a = self.wf("reusable-ux-one-template.yml")["jobs"]["ux-one-template"]["steps"]
        b = self.wf("ux-required.yml")["jobs"]["ux-one-template"]["steps"]
        fetch = lambda ss: next(s["with"] for s in ss if s.get("with", {}).get("repository") == "purpusgit/.github")
        run = lambda ss: next(s["run"] for s in ss if "ux_one_template.py" in s.get("run", ""))
        self.assertEqual(fetch(a), fetch(b))
        self.assertEqual(run(a), run(b))
        self.assertIn("scripts/ux_one_template_allowlist.txt", fetch(b)["sparse-checkout"])  # the allowlist comes from main too
        self.assertEqual(fetch(b)["ref"], "main")

    def test_base_comes_from_the_pr_and_a_repo_without_pubspec_is_a_no_op(self):
        b = self.wf("ux-required.yml")["jobs"]["ux-one-template"]["steps"]
        self.assertEqual(next(s["env"]["BASE"] for s in b if s.get("env", {}).get("BASE")), "${{ github.base_ref || 'cwb' }}")
        self.assertIn("pubspec.yaml", next(s for s in b if s.get("id") == "flutter")["run"])
        for s in b:
            if "ux_one_template.py" in s.get("run", "") or s.get("with", {}).get("repository") == "purpusgit/.github":
                self.assertIn("steps.flutter.outputs.yes == 'true'", s.get("if", ""), s.get("name"))


def run(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(base_files, head_files, pubspec="name: pkg_orbit_japa\n"):
    root = tempfile.mkdtemp(prefix="uxone-")
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
    def gate(self, root, base="base", repo_env="pkg_orbit_japa"):
        old, env = os.getcwd(), os.environ.get("GITHUB_REPOSITORY")
        os.chdir(root)
        os.environ["GITHUB_REPOSITORY"] = "purpusgit/" + repo_env
        try:
            return U.gate(base)
        finally:
            os.chdir(old)
            if env is None:
                os.environ.pop("GITHUB_REPOSITORY", None)
            else:
                os.environ["GITHUB_REPOSITORY"] = env

    FRAME = "w() => NavigationBar(a: 1);\n"

    def test_a_new_frame_fails_and_an_old_one_passes(self):
        self.assertEqual(self.gate(repo({"lib/old.dart": self.FRAME}, {"lib/old.dart": self.FRAME, "lib/n.dart": "w() => Text('x');\n"})), 0)
        self.assertEqual(self.gate(repo({}, {"lib/n.dart": self.FRAME})), 1)

    def test_a_pr_cannot_allow_itself_by_adding_an_allowlist_to_its_own_repo(self):
        bad = {"lib/n.dart": self.FRAME, "scripts/ux_one_template_allowlist.txt": "pkg_orbit_japa lib/n.dart NavigationBar\n"}
        self.assertEqual(self.gate(repo({}, bad)), 1)

    def test_an_allowlisted_shipped_pair_passes_only_in_its_repo(self):
        p = "lib/src/screens/shell/orbit_shell.screen.dart"
        self.assertEqual(self.gate(repo({}, {p: self.FRAME}, "name: pkg_orbit_binder\n"), repo_env="pkg_orbit_binder"), 0)
        self.assertEqual(self.gate(repo({}, {p: self.FRAME}), repo_env="pkg_orbit_japa"), 1)

    def test_client_core_may_build_the_shell(self):
        self.assertEqual(self.gate(repo({}, {"lib/shell/x.dart": self.FRAME}, "name: orbit_client_core\n"), repo_env="pkg_orbit_client_core"), 0)

    def test_generated_non_lib_and_comment_only_changes_pass(self):
        files = {"lib/a.g.dart": self.FRAME, "test/a_test.dart": self.FRAME, "lib/c.dart": "// NavigationBar( was removed\n"}
        self.assertEqual(self.gate(repo({}, files)), 0)

    def test_an_unresolvable_base_fails_closed(self):
        self.assertEqual(self.gate(repo({}, {"lib/a.dart": "x;\n"}), "no-such-ref"), 2)

    def test_a_missing_allowlist_fails_closed(self):
        old = U.ALLOWLIST
        U.ALLOWLIST = os.path.join(tempfile.mkdtemp(), "nope.txt")
        try:
            self.assertEqual(self.gate(repo({}, {"lib/a.dart": "x;\n"})), 2)
        finally:
            U.ALLOWLIST = old


if __name__ == "__main__":
    unittest.main(verbosity=1)
