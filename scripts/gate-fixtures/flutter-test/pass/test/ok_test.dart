import 'package:flutter_test/flutter_test.dart';

import 'package:gate_fixture_test_pass/ok.dart';

void main() {
  test('fixture suite passes', () {
    expect(add(1, 1), 2);
  });
}
