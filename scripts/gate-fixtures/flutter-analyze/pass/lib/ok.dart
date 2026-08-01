/// Clean library: no warnings, no errors. The gate must exit 0 here.
///
/// Deliberately contains a normal (unescaped) interpolation so this fixture
/// also documents what correct interpolation looks like next to the FAIL case.
int add(int a, int b) => a + b;

String describe(int a, int b) => 'sum of $a and $b is ${add(a, b)}';
