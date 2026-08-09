# purpusgit/.github

Org-level GitHub Actions reusable workflows for all `purpusgit` repositories.

> **Every consumer resolves these `@main`.** An edit merged here is live across
> the org immediately — there is no per-repo pinning and no staged rollout. Treat
> a change to any gate below as a change to every repo that calls it.

## Reusable gates

| Workflow | Checks |
|---|---|
| `reusable-flutter-analyze.yml` | `flutter analyze lib/ --no-fatal-infos` for `pkg_*` packages |
| `reusable-barrel-safety-gate.yml` | Barrel/factory export removal (Rule 66) |
| `reusable-colors-safety-gate.yml` | Hardcoded `Colors.*`, `withOpacity`, non-null `extension<>()!` on added lines (Rule 24) |
| `reusable-dart-safety-gate.yml` | Escaped Dart string interpolation — `\${x}` renders as literal text |
| `reusable-rule84-flavor-fork-gate.yml` | Flavour config & fork discipline (Rule 84) |
| `reusable-tsc-check.yml` | `tsc --noEmit` for TypeScript service repos |
| `reusable-sql-safety-gate.yml` | Stray semicolons in SQL template literals |
| `reusable-sql-typestring-safety-gate.yml` | PascalCase `taxo.master` type strings (Rules 58/59) |
| `reusable-taxo-lint.yml` | Taxonomy type/column lint — delegates to `scripts/taxo_lint.py` |
| `reusable-taxo-contract-lint.yml` | Taxonomy FK contract: wrong `hierarchy_level` / wrong concept |
| `reusable-drizzle-journal-gate.yml` | Every Drizzle `.sql` migration has a `meta/_journal.json` entry |
| `taxo-data-lint-nightly.yml` | Scheduled DB-backed taxonomy data lint |

Consumers call these from a thin caller workflow. The reported check-run context
is `<caller job id> / <reusable job name>` — for example `analyze / flutter analyze`.
**That string is what branch protection requires; renaming either half breaks it.**

## Self-CI

`self-test-gates.yml` runs `scripts/gate_selftest.py`, which extracts each gate's
**live** `run:` predicate, syntax-checks it, and exercises the self-contained ones
against pass/fail fixtures in `scripts/gate-fixtures/`.

Two properties worth knowing before you add a gate here:

- **It fails closed on an unmapped gate.** A new workflow without a `BEHAVIOUR`
  entry in `gate_selftest.py` reds self-CI — by design, so a gate cannot join the
  canon without a decision about how it is tested.
- **A gate that has never gone red in a fixture has not been tested.** Prove
  red-detection with the offline fixtures and the in-harness canary, never by
  pushing a broken predicate to a real gate on `main` — consumers resolve `@main`,
  so that is an org-wide outage, not a local test.

## Docs

- `docs/rules-to-gates-rollout.md` — gate inventory, scan scopes, caller coverage
  and the standing invariants.
