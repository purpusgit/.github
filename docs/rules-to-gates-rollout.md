# Rules-to-Gates Rollout

Canonical record of the org's machine-enforced coding-rule gates: what each gate
checks, its scan scope, which repos call it, and the standing invariants. Lives in
`purpusgit/.github` beside the gates it documents. Update this file in the same PR
that changes a gate or wires a repo.

_Last updated: 2026-08-01._

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
| `reusable-taxo-lint.yml` → `scripts/taxo_lint.py --code` | G1 PascalCase type + G2 bad columns | `**/*.sql` + template-scoped `**/*.ts` (excl `backups`/`dumps`/tests) | **Yes** — template-scoped + only fires inside a `taxo.master` backtick template | Advisory (`--code`); `--data` is DB-backed |
| `reusable-taxo-contract-lint.yml` → `taxo-contract/checker.py` | Wrong `hierarchy_level` / wrong concept (rule 2.2) | `src/**/*.sql.ts`, validators | Yes | **0 callers — dead** (see Open items) |
| `reusable-sql-safety-gate.yml` | Stray semicolons in SQL template literals | `src/**/*.sql.ts` | n/a | Blocking |
| `reusable-colors-safety-gate.yml` | Hardcoded `Colors.*`, `withOpacity`, `extension<>()!` | added lines in `lib/**/*.dart` | Diff-only | Blocking |
| `reusable-dart-safety-gate.yml` | Escaped `\${` interpolation | `lib/**/*.dart` | n/a | Blocking |
| `taxo-data-lint-nightly.yml` | FK:<Type> staleness (DB) | scheduled in `.github` | n/a | Nightly; metric = last-successful-run, **not** caller count |

(`reusable-host-pin-autobump.yml` was **removed** 2026-08-01 in PR #18 — 0 callers, superseded by `main_org_orbit`'s `roll-internal-deps.yaml`.)

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
| `taxo-lint` (advisory) | `service_orbit_orgs`, `service_auth` (#188), `service_inapp_chat` (#78), `service_goalcaller_voicereach` (#44) | inapp_chat/voicereach use `code_dir: "."` (taxo lives in root `migrations/` + route `.ts`, not `src/`); all dispatch-verified |
| `taxo-contract-lint` | **none** | Detector correct, unwired — dead |

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

- **The two `flutter test` failures PR #20 surfaced — never recorded until now, both still live.**
  PR #20 (`af225dce3e`, 2026-08-02) removed the mandatory `flutter test` step from
  `reusable-flutter-analyze.yml` and stated the two genuine failures it had found were
  *"recorded in the doc's Open items so removing the step does not lose them"*. They were not
  written here. Recorded now, and **both were re-verified as still failing on 2026-08-19**:

  1. **`pkg_orbit_marketplace`** — `test/orbit_marketplace_test.dart:7:24: Error: Method not
     found: 'Calculator'.` Still present on `cwb` today: the file is the unmodified
     `flutter create` template test, calling `Calculator().addOne(...)` against a symbol
     `package:orbit_marketplace/orbit_marketplace.dart` does not export. It has never
     compiled. Fix is to delete or rewrite the template stub — this is not product debt.
  2. **`pkg_orbit_broadcast`** — `test/screens/composer_screen_test.dart`: `RenderFlex`
     overflow + `Found 0 widgets with text "Message"`. Re-confirmed 2026-08-19 on
     `pkg_orbit_broadcast` PR #31 (check run 96137... , job log): **27 failed / 44 passed**,
     all 27 in that one file. `A RenderFlex overflowed by 424 pixels on the right`, raised
     from `lib/screens/composer/composer.screen.dart:399:17` inside `_HeroPreview`; the
     overflow then prevents the sheet-opening taps from finding their targets
     (`Found 0 widgets with text "Message"` ×12, `"Audience"` ×6, `"Channel"` ×3,
     `"Advanced"` ×3). The other four suites in that repo pass. This is a real layout
     defect at small widths, not test-harness noise.

- **Per-repo `flutter test` gate rollout — in flight.** The replacement the removal note
  proposed (*"it belongs in its own declared reusable workflow, piloted and rolled out on its
  own merit"*) is being adopted as a repo-local `.github/workflows/flutter_test.yml` reporting
  under job name `flutter test`. Live in `pkg_orbit_japa` and `pkg_orbit_inapp_purchases`
  (`0c4add7638`). Open PRs 2026-08-19: `pkg_orbit_client_core` #555 (green),
  `pkg_inapp_chat` #91 (green), `pkg_orbit_auth` #139 (green), `pkg_orbit_binder` #188,
  `pkg_orbit_broadcast` #31 (red — item 2 above). Not yet a reusable workflow: five repos of
  identical content is the threshold to lift it, and that lift should happen once the red
  ones are resolved.

  Two mechanical findings worth keeping from that rollout:
  - **No `paths:` filter.** A required check that is skipped never reports, so GitHub blocks
    the PR on *"Expected — waiting for status"* indefinitely. It also means the gate runs on
    the PR that introduces it.
  - **No `branches:` list under `pull_request`.** That list matches the PR's *base*, so a
    stacked PR gets zero check runs. All five `flutter_analyze.yml` callers still carry
    `branches: [cwb, dev, master]` and have this defect today.

- **Bug #3 (`taxo-contract-lint`)** — checker rule 2.2 is correct and block-scoped
  (commit `6a5637d2`) but the gate has **0 callers**. Wiring blocked until the
  annotation-mandate question and a pilot repo are settled.

## Recently done

- **`taxo_lint.py` glob broadening merged** (PR #17, Fable-reviewed 2026-08-01) —
  `**/*.sql` migrations + template-scoped inline SQL in `.ts`, with `_mask_ts_noncode`
  (masks JS comments/strings so a stray backtick can't desync template parity).
  Guarded by `scripts/test_taxo_lint.py` (23 checks incl. a pinned M9 KNOWN-GAP:
  regex-literal backtick is a documented miss). Rolled to `service_inapp_chat` (#78)
  and `service_goalcaller_voicereach` (#44), both dispatch-verified.
- **`host-pin-autobump` removed** (PR #18, 2026-08-01) — 0 callers, superseded by
  `main_org_orbit`'s `roll-internal-deps.yaml` (native `flutter pub upgrade`, "the
  clean replacement for the retired lock-refresh workflow"). `scripts/host_pin_integrity.py`
  left in place (may serve a separate integrity gate).

## Governance memories

`platform/git`: `fe7fd907` (rules-to-gates promotion doctrine), `0cc64b3a`
(zero-caller gate = unenforced; caller-count metric with the scheduled-job exception).
Rule-ID citations only; no filenames (doctrine §8a).
