#!/usr/bin/env python3
"""Self-test for scripts/http_headers.py: every header-writing pattern failing and passing, the escape, the exempt directory,
added-lines scoping, the client_core guard pins, the gate end to end on throwaway git repos, and the workflow pins. No network.
Run: python3 scripts/test_http_headers.py"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import http_headers as H  # noqa: E402


def hits(src, path="lib/a.dart", repos=("pkg_orbit_japa",), added=None, exempt=H.DEFAULT_EXEMPT):
    return [k for _, k, _ in H.scan_text(path, "void f(){ %s }\n" % src if "\n" not in src else src, added, repos, exempt)]


class RuleA(unittest.TestCase):
    def test_index_assignment(self):
        self.assertEqual(hits("req.headers['x-org'] = org.name;"), ["headers[]="])
        self.assertEqual(hits("req.headers['x-device'] = 'ios';"), [])
        self.assertEqual(hits("options.headers['Authorization'] = authHeader;"), ["headers[]="])
        self.assertEqual(hits("reqHeaders['x'] ??= name;"), ["headers[]="])

    def test_interpolation_is_a_non_literal_but_raw_and_plain_strings_are_fine(self):
        self.assertEqual(hits("h.headers['A'] = 'Bearer $token';"), ["headers[]="])
        self.assertEqual(hits("h.headers['A'] = 'Bearer ${t.value}';"), ["headers[]="])
        self.assertEqual(hits("h.headers['A'] = 'Bearer ' + token;"), ["headers[]="])
        self.assertEqual(hits("h.headers['A'] = 'price \\$5';"), [])
        self.assertEqual(hits("h.headers['A'] = r'raw $x';"), [])
        self.assertEqual(hits("h.headers['A'] = 'a' 'b';"), [])

    def test_constants_and_literals_are_fine(self):
        for v in ("kOrbitFlavorCodeHeader", "HEADER_VALUE", "Headers.jsonContentType", "HttpHeaders.contentTypeHeader",
                  "ContentType.json.mimeType", "const 'x'", "null", "true", "42"):
            self.assertEqual(hits("h.headers['k'] = %s;" % v), [], v)

    def test_ternary_and_null_coalescing_judge_the_branches_not_the_test(self):
        self.assertEqual(hits("h.headers['k'] = isWeb ? 'a' : 'b';"), [])
        self.assertEqual(hits("h.headers['k'] = isWeb ? 'a' : name;"), ["headers[]="])
        self.assertEqual(hits("h.headers['k'] = x ?? 'd';"), ["headers[]="])
        self.assertEqual(hits("h.headers['k'] = kA ?? 'd';"), [])

    def test_set_add_put_if_absent(self):
        self.assertEqual(hits("r.headers.set('x', value);"), ["headers.set"])
        self.assertEqual(hits("r.headers.add('x', value);"), ["headers.add"])
        self.assertEqual(hits("r.headers.add('x', 'lit');"), [])
        self.assertEqual(hits("r.headers.putIfAbsent('A', () => 'Bearer $t');"), ["headers.putIfAbsent"])
        self.assertEqual(hits("r.headers.putIfAbsent('A', () => 'lit');"), [])

    def test_add_all(self):
        self.assertEqual(hits("h.headers.addAll({'a': 'x', 'b': name, 'c': 'lit'});"), ["headers.addAll{}"])
        self.assertEqual(hits("h.headers.addAll({'a': 'x'});"), [])
        self.assertEqual(hits("h.headers.addAll(other);"), ["headers.addAll(x)"])
        self.assertEqual(hits("h.headers.addAll(const {'a': 'x'});"), [])

    def test_headers_named_argument(self):
        self.assertEqual(hits("dio.get(u, options: Options(headers: {'a': 'x', 'b': user.email}));"), ["headers:{}"])
        self.assertEqual(hits("http.get(u, headers: {'Accept': 'application/json'});"), [])
        self.assertEqual(hits("http.get(u, headers: const {'a': 'b'});"), [])
        self.assertEqual(hits("http.get(u, headers: <String, String>{'a': 'b'});"), [])
        self.assertEqual(hits("http.get(u, headers: h);"), ["headers:x"])
        self.assertEqual(hits("http.get(u, headers: {...base, 'a': 'b'});"), ["headers:{}"])
        self.assertEqual(hits("fetch(u, FetchOptions(headers: {'x-name': org.shortName}));"), ["headers:{}"])

    def test_reads_comparisons_comments_and_strings_are_not_writes(self):
        for src in ("if (r.headers['x'] == null) {}", "if (r.headers['y'] != y) {}", "final v = r.headers['z'];",
                    "print('r.headers[\"x\"] = name;');", "// r.headers['x'] = name;"):
            self.assertEqual(hits(src), [], src)

    def test_the_escape_needs_a_reason(self):
        self.assertEqual(hits("void f(){ h.headers['x-id'] = org.id; // header-ok: numeric id\n}"), [])
        self.assertEqual(hits("// header-ok: ascii id\nvoid f(){ h.headers['x-id'] = org.id; }"), [])
        self.assertEqual(hits("void f(){ h.headers['x-id'] = org.id; // header-ok:\n}"), ["headers[]="])
        self.assertEqual(hits("final z = 1; // header-ok: x\nvoid f(){ h.headers['x-id'] = org.id; }"), ["headers[]="])

    def test_a_multiline_value_reports_the_line_it_starts_on(self):
        t = "void f() {\n  h.headers['x'] =\n      org.name;\n}\n"
        self.assertEqual([n for n, _, _ in H.scan_text("lib/a.dart", t, None, ("r",))], [2])

    def test_only_added_lines_count(self):
        t = "void f() {\n  a.headers['x'] = n1;\n  b.headers['y'] = n2;\n}\n"
        self.assertEqual([n for n, _, _ in H.scan_text("lib/a.dart", t, {3}, ("r",))], [3])
        self.assertEqual(H.scan_text("lib/a.dart", t, set(), ("r",)), [])


class Exempt(unittest.TestCase):
    SRC = "void f(){ h.headers['x'] = n; }"

    def test_client_core_lib_http_is_exempt_and_nothing_else(self):
        self.assertEqual(hits(self.SRC, "lib/http/x.dart", ("pkg_orbit_client_core",)), [])
        self.assertEqual(hits(self.SRC, "lib/ui/x.dart", ("pkg_orbit_client_core",)), ["headers[]="])
        self.assertEqual(hits(self.SRC, "lib/http/x.dart", ("pkg_orbit_japa",)), ["headers[]="])  # another repo's lib/http is not exempt

    def test_the_exempt_list_is_configurable(self):
        self.assertEqual(hits(self.SRC, "lib/net/x.dart", ("pkg_orbit_japa",), exempt="pkg_orbit_japa:lib/net/"), [])
        self.assertEqual(hits(self.SRC, "lib/http/x.dart", ("pkg_orbit_client_core",), exempt="pkg_orbit_client_core:lib/other/"), ["headers[]="])


GOOD_ORG = """
import 'x.dart';
Map<String, String> flavorCodeHeaders() => const {};
void applyOrgHeaders(Map<String, String> headers, OrgState? org) {
  if (org?.orgId != null) headers['x-org-id'] = '${org!.orgId}';
  final shortName = org?.orgShortName;
  if (shortName != null && isHeaderSafe(shortName)) headers['x-org-short-name'] = shortName;
}
/// True when every character fits ISO-8859-1.
bool isHeaderSafe(String value) => value.codeUnits.every((c) => c <= 0xFF);
"""
INTERCEPTOR = "class CachedHttpInterceptor { void go(r) { applyOrgHeaders(r.headers, org); } }\n"


def files(**over):
    f = {"lib/http/org_headers.dart": GOOD_ORG, "lib/http/_http_utils_web.dart": INTERCEPTOR, "lib/http/_http_utils_mobile.dart": INTERCEPTOR}
    f.update({k.replace("__", "/"): v for k, v in over.items()})
    return f


def probs(fs):
    return H.pins_problems(lambda p: fs.get(p))


class RuleB(unittest.TestCase):
    def test_the_good_files_pass(self):
        self.assertEqual(probs(files()), [])

    def test_guard_removed(self):
        bad = GOOD_ORG.replace("bool isHeaderSafe(String value) => value.codeUnits.every((c) => c <= 0xFF);", "")
        self.assertTrue(any("guard `bool isHeaderSafe(` is gone" in p for p in probs({**files(), "lib/http/org_headers.dart": bad})))

    def test_guard_weakened_so_it_no_longer_tests_0xff(self):
        bad = GOOD_ORG.replace("value.codeUnits.every((c) => c <= 0xFF)", "true")
        self.assertTrue(any("no longer tests for 0xFF" in p for p in probs({**files(), "lib/http/org_headers.dart": bad})))

    def test_a_block_bodied_guard_is_read_whole(self):
        ok = GOOD_ORG.replace("bool isHeaderSafe(String value) => value.codeUnits.every((c) => c <= 0xFF);",
                              "bool isHeaderSafe(String value) {\n  for (final c in value.codeUnits) {\n    if (c > 0xFF) return false;\n  }\n  return true;\n}")
        self.assertEqual(probs({**files(), "lib/http/org_headers.dart": ok}), [])

    def test_short_name_written_without_the_guard(self):
        bad = GOOD_ORG.replace("if (shortName != null && isHeaderSafe(shortName)) headers['x-org-short-name'] = shortName;",
                               "if (shortName != null) headers['x-org-short-name'] = shortName;")
        self.assertTrue(any("written without isHeaderSafe" in p for p in probs({**files(), "lib/http/org_headers.dart": bad})))

    def test_the_guard_on_the_line_above_counts(self):
        ok = GOOD_ORG.replace("if (shortName != null && isHeaderSafe(shortName)) headers['x-org-short-name'] = shortName;",
                              "if (shortName != null && isHeaderSafe(shortName))\n    headers['x-org-short-name'] = shortName;")
        self.assertEqual(probs({**files(), "lib/http/org_headers.dart": ok}), [])

    def test_the_write_vanishing_is_a_problem_not_a_pass(self):
        bad = GOOD_ORG.replace("headers['x-org-short-name'] = shortName;", "")
        self.assertTrue(any("no write of `x-org-short-name`" in p for p in probs({**files(), "lib/http/org_headers.dart": bad})))

    def test_an_interceptor_that_stops_calling_apply_org_headers(self):
        self.assertTrue(any("_http_utils_web.dart no longer calls applyOrgHeaders(" in p
                            for p in probs({**files(), "lib/http/_http_utils_web.dart": "class X {}\n"})))

    def test_a_missing_pinned_file_is_reported(self):
        fs = files(); del fs["lib/http/org_headers.dart"]
        self.assertTrue(any("is missing" in p for p in probs(fs)))


def run(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(base_files, head_files, pubspec="name: pkg_orbit_japa\n"):
    root = tempfile.mkdtemp(prefix="httph-")
    run("git", "init", "-q", "-b", "base", cwd=root)
    for step, fs in (("base", base_files), ("head", head_files)):
        if step == "head":
            run("git", "checkout", "-qb", "head", cwd=root)
        for rel, body in {**{"pubspec.yaml": pubspec}, **fs}.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(body)
        run("git", "add", "-A", cwd=root)
        run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", step, cwd=root)
    return root


class Gate(unittest.TestCase):
    def gate(self, root, base="base", repo_env="pkg_orbit_japa"):
        old = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "purpusgit/" + repo_env
        cwd = os.getcwd()
        os.chdir(root)
        try:
            return H.gate(base, root)
        finally:
            os.chdir(cwd)
            os.environ.pop("GITHUB_REPOSITORY", None) if old is None else os.environ.update(GITHUB_REPOSITORY=old)

    BAD = "void f(r, o) { r.headers['x-org'] = o.name; }\n"

    def test_an_old_blunder_passes_and_a_new_one_fails(self):
        self.assertEqual(self.gate(repo({"lib/old.dart": self.BAD}, {"lib/old.dart": self.BAD, "lib/n.dart": "void g(){}\n"})), 0)
        self.assertEqual(self.gate(repo({}, {"lib/n.dart": self.BAD})), 1)

    def test_a_line_added_to_an_old_function_only_counts_if_it_is_the_blunder(self):
        base = "void f(r, o) {\n  r.headers['a'] = 'x';\n}\n"
        self.assertEqual(self.gate(repo({"lib/a.dart": base}, {"lib/a.dart": base.replace("'x';", "'x';\n  r.headers['b'] = 'y';")})), 0)
        self.assertEqual(self.gate(repo({"lib/a.dart": base}, {"lib/a.dart": base.replace("'x';", "'x';\n  r.headers['b'] = o.name;")})), 1)

    def test_generated_and_non_lib_files_are_ignored(self):
        self.assertEqual(self.gate(repo({}, {"lib/a.g.dart": self.BAD, "test/a_test.dart": self.BAD})), 0)

    def test_an_unresolvable_base_fails_closed(self):
        self.assertEqual(self.gate(repo({}, {"lib/a.dart": "x;\n"}), "no-such-ref"), 2)

    def test_client_core_runs_the_pins_and_other_repos_do_not(self):
        self.assertEqual(self.gate(repo({}, files(), "name: orbit_client_core\n"), repo_env="pkg_orbit_client_core"), 0)
        broken = files(lib__http__org_headers_dart=GOOD_ORG.replace("isHeaderSafe(shortName)", "true"))
        broken["lib/http/org_headers.dart"] = GOOD_ORG.replace("isHeaderSafe(shortName)", "true")
        self.assertEqual(self.gate(repo({}, broken, "name: orbit_client_core\n"), repo_env="pkg_orbit_client_core"), 1)
        self.assertEqual(self.gate(repo({}, {"lib/a.dart": "void g(){}\n"}), repo_env="pkg_orbit_japa"), 0)

    def test_client_core_pins_also_run_when_a_pr_touches_nothing_in_http(self):
        fs = files(); fs["lib/ui/a.dart"] = "void g(){}\n"; del fs["lib/http/org_headers.dart"]
        self.assertEqual(self.gate(repo({}, fs, "name: orbit_client_core\n"), repo_env="pkg_orbit_client_core"), 2)  # missing pinned file: fail closed


class Workflows(unittest.TestCase):
    @staticmethod
    def wf(name):
        import yaml
        with open(os.path.join(HERE, "..", ".github", "workflows", name)) as fh:
            return yaml.safe_load(fh)

    def test_the_standalone_job_runs_the_same_script_as_the_reusable(self):
        a = self.wf("reusable-http-headers.yml")["jobs"]["http-headers"]["steps"]
        b = self.wf("ux-required.yml")["jobs"]["http-headers"]["steps"]
        fetch = lambda ss: next(s["with"] for s in ss if s.get("with", {}).get("repository") == "purpusgit/.github")
        run = lambda ss: next(s["run"] for s in ss if "http_headers.py" in s.get("run", ""))
        self.assertEqual(fetch(a), fetch(b))
        self.assertEqual(run(a), run(b))
        self.assertEqual(fetch(b)["ref"], "main")
        self.assertEqual(fetch(b)["sparse-checkout"].strip(), "scripts/http_headers.py")
        self.assertIn("python3 -I", run(b))

    def test_base_branch_and_no_op_without_pubspec(self):
        for name in ("reusable-http-headers.yml", "ux-required.yml"):
            steps = self.wf(name)["jobs"]["http-headers"]["steps"]
            base = next(s["env"]["BASE"] for s in steps if s.get("env", {}).get("BASE"))
            self.assertIn("github.base_ref", base)
        steps = self.wf("ux-required.yml")["jobs"]["http-headers"]["steps"]
        self.assertIn("pubspec.yaml", next(s for s in steps if s.get("id") == "flutter")["run"])
        for s in steps:
            if "http_headers.py" in s.get("run", "") or s.get("with", {}).get("repository") == "purpusgit/.github":
                self.assertIn("steps.flutter.outputs.yes == 'true'", s.get("if", ""), s.get("name"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
