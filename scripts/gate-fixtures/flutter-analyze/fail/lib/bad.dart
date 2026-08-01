/// Plants a WARNING-level diagnostic (`unused_element`), not an error and not
/// an info.
///
/// This is the exact class of finding the gate must still catch: the invocation
/// is `flutter analyze lib/ --no-fatal-infos`, so infos are deliberately not
/// fatal while warnings are. A fixture planting a hard error would pass even if
/// someone loosened the gate to `--no-fatal-warnings`; this one would not.
/// Same diagnostic that reds pkg_orbit_create_configure today.
int add(int a, int b) => a + b;

int _neverReferenced(int a) => a;
