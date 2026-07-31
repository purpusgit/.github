# Rules-to-Gates Rollout

Canonical record of the org's machine-enforced coding-rule gates: what each gate
checks, its scan scope, which repos call it, and the standing invariants. Lives in
`purpusgit/.github` beside the gates it documents. Update this file in the same PR
that changes a gate or wires a repo.

_Last updated: 2026-07-31._

---

## Principle

A mechanical rule that nothing executes is guidance, not a rule — it drifts silently.
A rule becomes enforced only when (1) its predicate lives as a reusable workflow in
this repo, (2) a thin caller wires it into each consumer repo, and (3) it has been
proven to go **red** on a planted fixture. A green pilot is not a rollout; enforcement
is measured by **caller count**, not by the gate's existence.

---

## Gate inventory and scope

| Gate (reusable workflow) | Checks | Scan scope | Context-gated? | Mode |
|---|---|---|---|---|
| `reusable-sql-typestring-safety-gate.yml` | PascalCase `taxo.master` `type = '...'` (Rule 58/59) | `*.sql.ts` **only** | **No** — plain grep | Diff-only on PRs; `full_scan` for audits |
| `reusable-taxo-lint.yml` → `scripts/taxo_lint.py --code` | G1 PascalCase type + G2 bad columns | `**/*.sql.ts` (broadening pending, see below) | **Yes** — only fires near a `taxo.master` mention | Advisory (`--code`); `--data` is DB-backed |
| `reusable-taxo-contract-lint.yml` → `taxo-contract/checker.py` | Wrong `hierarchy_level` / wrong concept (rule 2.2) | `src/**/*.sql.ts`, validators | Yes | **0 callers — dead** (see Open items) |
| `reusable-sql-safety-gate.yml` | Stray semicolons in SQL template literals | `src/**/*.sql.ts` | n/a | Blocking |
| `reusable-colors-safety-gate.yml` | Hardcoded `Colors.*`, `withOpacity`, `extension<>()!` | added lines in `lib/**/*.dart` | Diff-only | Blocking |
| `reusable-dart-safety-gate.yml` | Escaped `\${` interpolation | `lib/**/*.dart` | n/a | Blocking |
| `reusable-host-pin-autobump.yml` | Rolls host pubspec pins on pkg push | n/a | n/a | **0 callers — dead** |
| `taxo-data-lint-nightly.yml` | FK:<Type> staleness (DB) | scheduled in `.github` | n/a | Nightly; metric = last-successful-run, **not** caller count |

**Two gates, two scopes (do not merge).** The bash `sql-typestring` gate has **no**
context-gating — its safety is entirely that its glob is SQL-only file types. The
python `taxo-lint` gate **is** `taxo.master`-context-gated, so it (and only it) may
scan a broader set of files. See invariants.

## Self-CI

`purpusgit/.github` runs its own CI: `.github/workflows/self-test-gates.yml` →
`scripts/gate_selftest.py` extracts each gate's live `run:` predicate, `bash -n`s it,
and runs the self-contained gates against pass/fail fixtures with a canary that proves
the fail-assertions can fail. Checker logic is guarded by offline tests
(`taxo-contract/test_checker.py`, `scripts/test_taxo_lint.py`). Never prove red-detection
by breaking a real gate on `main` — consumers resolve these workflows `@main`, so a
deliberate break is an org-wide outage.

---

## Caller coverage (2026-07-31)

Broadly enforced: `dart-safety` (17 repos), `sql-safety` (17), `colours` (14).

| Gate | Wired repos | Notes |
|---|---|---|
| `sql-typestring` | `service_inapp_chat` (#77), `service_auth` (#188), `service_orbit_orgs` (#1127) | Diff-only; safe to wire anywhere |
| `taxo-lint` (advisory) | `service_orbit_orgs`, `service_auth` (#188) | `service_auth` verified via dispatch run 30667001320 |
| `taxo-contract-lint` | **none** | Detector correct, unwired — dead |
| `host-pin-autobump` | **none** | Dead / superseded (see Open items) |

Genuine taxo `*.sql.ts` surface exists only in `service_auth` and `service_orbit_orgs`
(and, once broadened, the migration/inline-SQL surfaces of `service_inapp_chat` and
`service_goalcaller_voicereach`).

---

## Convention (target end-state)

**New taxo SQL belongs in `*.sql.ts` files.** The broadened `taxo-lint` glob
(`**/*.sql` migrations + template-scoped inline SQL in `.ts`) is the **net for what
already exists**, not permission to keep writing inline SQL in service/route files.
Reviewers should push new taxo queries into `*.sql.ts`.

---

## Standing invariants (do NOT violate)

1. **`sql-typestring` glob stays SQL-only.** It has no context-gating; widening it to
   `.ts` (or any non-SQL type) recreates the spec-file false-positive class (proven:
   `const type = 'Fixture_Type'` in a test file). Broader/inline-SQL surfaces are owned
   by `taxo-lint`. Do not add `*.sql` to it either — `taxo-lint` owns migrations, and
   two gates flagging the same line is duplicate noise with two suppression tokens.
2. **A gate that cannot diff must red, not pass.** The `sql-typestring` PR path
   `rev-parse --verify`s the base ref and fails if it can't resolve it — no silent
   vacuous pass.
3. **Paths-filtered callers must never be added to required status checks.** A required
   context that skips never reports, deadlocking every non-matching PR on
   "Expected — waiting for status" (cost `pkg_orbit_client_core` #494/#496). Use
   `workflow_dispatch` to force a verification run instead.
4. **Do not roll a CI change to N repos before it is green on 1.**
5. **No vacuous wiring.** Do not wire a gate onto a repo whose real taxo surface is
   outside the gate's glob — it reports green while guarding nothing.

---

## Exclusions

- **`service_goalcaller_contact_graph`** — no taxo gate. Verified zero taxonomy usage
  on 2026-07-31, not a false zero:
  - positive control: `search_code "_idfr" repo:purpusgit/service_goalcaller_contact_graph` → 7 files (index reaches the repo)
  - `search_code "taxo" repo:purpusgit/service_goalcaller_contact_graph` → 0
  - Recheck mechanism: the org-wide `full_scan` audit sweep re-catches it if it ever grows a taxo query.

---

## Open items

- **`taxo_lint.py` glob broadening (decisions 1 + 2a)** — **draft PR #17 open**,
  self-CI green (13-check regression `scripts/test_taxo_lint.py` + fixtures). Awaiting
  Fable's parser line-review before merge. Adds migrations `*.sql` (excl `backups`/
  `dumps`) + template-scoped inline SQL in `.ts` (excl spec/test).
- **Bug #3 (`taxo-contract-lint`)** — checker rule 2.2 is correct and block-scoped
  (commit `6a5637d2`) but the gate has **0 callers**. Wiring blocked until the
  annotation-mandate question and a pilot repo are settled.
- **`host-pin-autobump`** — 0 callers, **parked / superseded** (verified 2026-07-31).
  The live host-pin mechanism is `main_org_orbit/.github/workflows/roll-internal-deps.yaml`
  (`flutter pub upgrade` of internal `cwb` packages → PR → gated by flutter-analyze +
  barrel-safety), which its own header calls "the clean replacement for the retired
  lock-refresh workflow". Pins are **not** going stale; the org moved from SHA-pin
  bumping to native pub upgrade. `host-pin-autobump.yml` is dead code — candidate for
  deletion from `.github`.
- **`service_orbit_orgs` legacy count** — run its first `full_scan` to get the exact
  pre-existing PascalCase count (non-blocking).

## Governance memories

`platform/git`: `fe7fd907` (rules-to-gates promotion doctrine), `0cc64b3a`
(zero-caller gate = unenforced; caller-count metric with the scheduled-job exception).
Rule-ID citations only; no filenames (doctrine §8a).
