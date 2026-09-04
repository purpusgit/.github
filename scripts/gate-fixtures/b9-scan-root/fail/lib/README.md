CONSTRAINT: a zero-file scan must FAIL, not pass.

This tree is a Dart package -- pubspec.yaml is right there -- whose lib/ holds no
.dart file at all. That is a wrong scan root, and the org has already paid for
the alternative once: a shared gate whose root was wrong reported green having
read nothing, which is strictly worse than no gate, because it also publishes a
tick. The pass/ tree beside this one is the same package WITH a .dart file, so
the pair asserts the guard and not the absence of a violation.
