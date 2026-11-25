#!/usr/bin/env python3
"""Self-test for feature_gate_scan.py (FEATURE-GATE-1). Run: python3 scripts/test_feature_gate_scan.py"""
import json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feature_gate_scan as fg  # noqa: E402


def rules(f, line):
    return [h['rule'] for h in fg.scan_line('r', f, 1, line)]


class Rules(unittest.TestCase):
    # JAPA-D01's real cases (specs#518 7175123605)
    def test_env_true_reads(self):
        self.assertEqual(rules('src/a.ts', "if (process.env.MENTEE_EXCEPTION_ENABLED === 'true') {"), ['T1'])
        self.assertEqual(rules('src/a.ts', "const on = process.env.TEMPLE_NOTICE_COLUMN_READY === 'true';"), ['T1'])
        self.assertEqual(rules('src/a.ts', "if (process.env.LEADER_SEND_ENABLED !== 'true') return res.status(501);"), ['T1'])

    def test_env_kill_switch_on_by_default_not_flagged(self):
        self.assertEqual(rules('src/a.ts', "if (process.env.LEADER_SEND_ENABLED !== 'false') {"), [])

    def test_dart_define_default_false(self):
        self.assertEqual(rules('lib/a.dart', "const x = bool.fromEnvironment('SHOW_MENTEES', defaultValue: false);"), ['D2'])
        self.assertEqual(rules('lib/a.dart', "const x = bool.fromEnvironment('SHOW_MENTEES', defaultValue: true);"), [])

    def test_d8_shrink_behind_flag(self):
        self.assertEqual(rules('lib/a.dart', "if (!kMenteesEnabled) return const SizedBox.shrink();"), ['D8'])
        self.assertEqual(rules('lib/a.dart', "child: config.mentorFeature ? MenteeCard() : const SizedBox.shrink(),"), ['D8'])

    def test_d8_data_guard_not_flagged(self):
        self.assertEqual(rules('lib/a.dart', "if (items.isEmpty) return const SizedBox.shrink();"), [])

    def test_d4_wins_over_d8(self):
        self.assertEqual(rules('lib/a.dart', "Visibility(visible: false, child: kFeatureEnabled ? A() : B())"), ['D4'])

    def test_t5_env_and_compose(self):
        self.assertEqual(rules('.env.example', 'SADHANA_TARGET_ENABLED=false'), ['T5'])
        self.assertEqual(rules('docker-compose.yaml', '      LINEAGE_TREE_EVENTS_ENABLED: "false"'), ['T5'])
        self.assertEqual(rules('docker-compose.yaml', '      FEATURE_X: ${FEATURE_X:-true}'), [])
        self.assertEqual(rules('.env', 'SADHANA_TARGET_ENABLED=true'), [])

    def test_code_rules_outside_src_ignored(self):
        self.assertEqual(rules('scripts/a.ts', "if (process.env.X_ENABLED === 'true') {"), [])

    def test_kdebugmode_logging_not_flagged(self):
        self.assertEqual(rules('lib/a.dart', "if (kDebugMode) debugPrint('x');"), [])


class Diff(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        g = lambda *a: subprocess.run(['git', *a], cwd=self.d, check=True, capture_output=True)
        self.g = g
        g('init', '-q'); g('config', 'user.email', 't@t'); g('config', 'user.name', 't')
        os.makedirs(os.path.join(self.d, 'src'))
        open(os.path.join(self.d, 'src/a.ts'), 'w').write('export const a = 1;\n')
        g('add', '.'); g('commit', '-qm', 'base')
        open(os.path.join(self.d, 'src/a.ts'), 'a').write("if (process.env.MENTEE_EXCEPTION_ENABLED === 'true') {}\n")
        open(os.path.join(self.d, '.env.example'), 'w').write('SADHANA_TARGET_ENABLED=false\n')
        g('add', '.'); g('commit', '-qm', 'hide')

    def run_diff(self, *extra):
        return subprocess.run([sys.executable, os.path.join(HERE, 'feature_gate_scan.py'), 'diff', self.d, 'HEAD^1..HEAD',
                               '--repo', 'r', *extra], capture_output=True, text=True)

    def test_hits_exit_3(self):
        r = self.run_diff()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(sorted(json.loads(l)['rule'] for l in r.stdout.splitlines()), ['T1', 'T5'])

    def test_approved_hits_pass(self):
        hits = [json.loads(l) for l in self.run_diff().stdout.splitlines()]
        reg = os.path.join(self.d, 'approved.txt')
        with open(reg, 'w') as fh:
            fh.write('# header\n')
            for h in hits:
                fh.write(f"{h['id']}\tr\t{h['rule']}\t{h['symbol']}\thttps://claude.ai/artifact/x\n")
        r = self.run_diff('--approved', reg, '--gh', '--block')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('approved', r.stdout)

    def test_block_mode_emits_errors(self):
        r = self.run_diff('--gh', '--block')
        self.assertEqual(r.returncode, 3)
        self.assertTrue(all(l.startswith('::error') for l in r.stdout.splitlines()), r.stdout)

    def test_bad_registry_line_refused(self):
        reg = os.path.join(self.d, 'bad.txt')
        open(reg, 'w').write('notanid\tr\tT1\tX\thttp://x\n')
        r = self.run_diff('--approved', reg)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('bad approvals line', r.stderr)


if __name__ == '__main__':
    unittest.main()
