import 'package:flutter_test/flutter_test.dart';

import 'package:gate_fixture_test_fail/ok.dart';

// The `reason:` string is the harness's assertion target. Matching on a token
// we control, rather than on whatever phrasing `flutter test --reporter compact`
// happens to use this SDK release, keeps the fixture from going red for a
// cosmetic upstream change — and keeps a missing toolchain's exit 127 from
// being scored as "the gate caught the violation".
void main() {
  test('planted failure', () {
    expect(add(1, 1), 3, reason: 'GATE_FIXTURE_PLANTED_FAILURE');
  });
}
