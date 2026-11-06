#!/usr/bin/env python3
"""Self-test for scripts/central_ui_gate.py: each rule passes its clean case and fails its broken one.
A throwaway git repo for base/head and a fake client_core; no network. Run: python3 scripts/test_central_ui_gate.py"""
import os, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import central_ui_gate as G  # noqa: E402

INDEX = """# Central UI
| Name | What |
|---|---|
| `TaxonomyPickerScreen` | Open `TaxonomyPickerRoutes.screen` (`/taxonomy-picker-screen`) |
| `EmptyStateWidget` | empty state |
"""
NAMES = "# GENERATED\nTaxonomyPickerScreen\nEmptyStateWidget\n"
EXC = """exceptions:
  - screen: GoalHomeScreen
    module: pkg_goals
    reason: flavour-visual
    admitted_by: https://github.com/purpusgit/pkg_orbit_client_core/pull/9
"""
LOCK = """packages:
  orbit_client_core:
    dependency: "direct main"
    description:
      ref: cwb
      resolved-ref: "abc1234def"
      url: "https://github.com/purpusgit/pkg_orbit_client_core"
    source: git
"""
PUBSPEC = "name: pkg_goals\ndependencies:\n  flutter:\n    sdk: flutter\n  get: ^4.6.0\n"
BASE_MANIFEST = "package: pkg_goals\nscreens:\n  OldScreen:\n    file: lib/old.dart\n    cap: reading\n"
OLD_COPY = "class TaxonomyPickerScreen extends StatelessWidget {}\n"  # today's copy: on the base already


def sh(*a, cwd):
    subprocess.run(list(a), cwd=cwd, check=True, capture_output=True)


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.tmp)
        self.write("pubspec.yaml", PUBSPEC)
        self.write("pubspec.lock", LOCK)
        self.write(G.MANIFEST, BASE_MANIFEST)
        self.write("lib/old.dart", "class OldScreen {}\n")
        self.write("lib/picker_copy.dart", OLD_COPY)
        sh("git", "init", "-q", "-b", "cwb", cwd=self.tmp)
        sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "base", "--allow-empty", cwd=self.tmp)
        sh("git", "add", "-A", cwd=self.tmp)
        sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", cwd=self.tmp)
        os.environ["BASE_REF"] = "cwb"
        self.fetched = []
        files = {"CENTRAL_UI.md": INDEX, "central_ui_names.txt": NAMES, "central_exceptions.yaml": EXC}

        def fake(path, ref, token):
            self.fetched.append((path, ref))
            return files[path]
        self._real, G.fetch_core = G.fetch_core, fake

    def tearDown(self):
        G.fetch_core = self._real
        os.chdir(self.old)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def add_row(self, name, extra):
        self.write(G.MANIFEST, BASE_MANIFEST + f"  {name}:\n    file: lib/new.dart\n    cap: reading\n{extra}")

    def test_no_change_passes_and_old_copy_is_grandfathered(self):
        self.assertEqual(G.main(), 0)
        self.assertEqual(self.fetched[0], ("CENTRAL_UI.md", "abc1234def"))  # the version the lock pins

    def test_g1_new_row_without_central_fails(self):
        self.add_row("NewScreen", "")
        self.write("lib/new.dart", "class NewScreen {}\n")
        self.assertEqual(G.main(), 1)

    def test_g1_central_row_used_passes(self):
        self.add_row("NewScreen", "    central: EmptyStateWidget\n")
        self.write("lib/new.dart", "import 'package:orbit_client_core/UI/empty_state_widget.dart';\n"
                                   "class NewScreen { final w = EmptyStateWidget(); }\n")
        self.assertEqual(G.main(), 0)

    def test_g1_central_row_not_imported_fails(self):
        self.add_row("NewScreen", "    central: EmptyStateWidget\n")
        self.write("lib/new.dart", "class NewScreen {}\n")
        self.assertEqual(G.main(), 1)

    def test_g1_unknown_central_row_fails(self):
        self.add_row("NewScreen", "    central: MadeUpWidget\n")
        self.write("lib/new.dart", "import 'package:orbit_client_core/x.dart';\nclass NewScreen { MadeUpWidget w; }\n")
        self.assertEqual(G.main(), 1)

    def test_g1_none_needs_exception_entry(self):
        self.add_row("NewScreen", '    central: "none: flavour-visual"\n')
        self.write("lib/new.dart", "class NewScreen {}\n")
        self.assertEqual(G.main(), 1)

    def test_g1_none_with_exception_entry_passes(self):
        self.add_row("GoalHomeScreen", '    central: "none: flavour-visual"\n')
        self.write("lib/new.dart", "class GoalHomeScreen {}\n")
        self.assertEqual(G.main(), 0)

    def test_g1_none_with_bad_reason_fails(self):
        self.add_row("GoalHomeScreen", '    central: "none: because"\n')
        self.write("lib/new.dart", "class GoalHomeScreen {}\n")
        self.assertEqual(G.main(), 1)

    def test_g2_new_widget_copy_fails(self):
        self.write("lib/mine.dart", "class EmptyStateWidget extends StatelessWidget {}\n")
        self.assertEqual(G.main(), 1)

    def test_g2_new_route_copy_fails(self):
        self.write("lib/routes.dart", "final p = GetPage(\n  name: '/taxonomy-picker-screen',\n  page: () => Foo(),\n);\n")
        self.assertEqual(G.main(), 1)

    def test_g2_wrapped_package_fails(self):
        self.write("pubspec.yaml", PUBSPEC + "  geocoding: ^3.0.0\n")
        self.assertEqual(G.main(), 1)

    def test_g2_removing_a_copy_passes(self):
        os.remove("lib/picker_copy.dart")
        self.assertEqual(G.main(), 0)

    def test_unreadable_input_refuses(self):
        def boom(path, ref, token):
            raise G.InputError("404")
        G.fetch_core = boom
        self.assertEqual(G.main(), 2)

    def test_client_core_itself_is_skipped(self):
        self.write("pubspec.yaml", "name: orbit_client_core\n")
        self.assertEqual(G.main(), 0)
        self.assertEqual(self.fetched, [])

    def test_lock_without_git_ref_uses_cwb(self):
        self.assertEqual(G.core_ref("packages:\n  get:\n    version: 1\n"), "cwb")


if __name__ == "__main__":
    unittest.main(verbosity=1)
