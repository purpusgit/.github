# Rules-to-Gates Rollout

Canonical record of the org's machine-enforced coding-rule gates: what each gate
checks, its scan scope, which repos call it, and the standing invariants. Lives in
`purpusgit/.github` beside the gates it documents. Update this file in the same PR
that changes a gate or wires a repo.

_Last updated: 2026-08-20._

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
| **`check-route-auth-coverage.js`** (M34 auth coverage) | Every route registration carries a recognised auth identifier, or an `auth-exceptions.json` entry with a status + reason | `src/api/**/*.{ts,js}`, **route registrations only** | n/a — regex, no parser | Blocking. **Not yet a reusable workflow — see Open items** |

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

### M34 auth-coverage gate — the three things its count does not mean

Piloted on `service_orbit_analytics` (#197), first rollout `service_org_broadcast` (#158).
It prints "N route(s) checked". Three limits on what that N means, all found by running it,
none of them visible from a green check:

1. **It cannot tell a live route from dead code.** It scans route *registrations*, not the
   mount graph. In `service_org_broadcast` it flagged `POST /webhook` in both
   `src/api/payments/payment/index.ts` and `src/api/payments/refund/index.ts` — two routers
   that nothing imports or mounts, so neither route is reachable. They still consume an
   allowlist entry and still count toward N. Anyone reading the number will assume it counts
   live surface. It does not.
2. **It only sees `src/api`, and only router-named variables.** Anything registered directly
   on `app` in `src/server.ts` is invisible — in `service_org_broadcast` that hides an
   unauthenticated `express.static` media mount and an unauthenticated DB-touching health
   route. Record such surfaces in the repo's own `auth-exceptions.json` under a `_comment`
   key (the script only looks keys up, never iterates them, so an extra key is inert).
3. **`tracked-gap` is not `intentional-public`.** The two statuses exist so an unexplained
   route cannot be waved through as fine. `service_org_broadcast` came back 57 routes, 1
   gated, 4 public with in-code evidence, **52 tracked gaps**. A green check there means "no
   route 58 slipped in", not "the service is protected". Any rollout PR states both numbers.

**Windows:** the script builds allowlist keys with `path.join`, so on win32 it emits
`src\api\...` against forward-slash JSON keys and reports every route as a violation. CI is
ubuntu, so no PR is affected; a local run on a developer machine is not to be trusted until
`walk()` normalises the separator.

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
3. **A required check is never `paths:`- or `branches:`-filtered. Delete the filter,
   never the requirement.** A filtered context that misses does not report `skipped` —
   it does not report AT ALL, and protection blocks on the ABSENCE. The pull request
   pins on "Expected — waiting for status" permanently; re-running, reopening and empty
   commits all do nothing, because there is nothing to re-run. The block is invisible to
   anything that looks for failures. Cost so far: `pkg_orbit_client_core` #494/#496,
   `service_org_broadcast` #150, `service_orbit_orgs` #1128 and #1137,
   `service_marketplace_ecom` #3.
   An earlier wording of this invariant read "paths-filtered callers must never be added
   to required status checks", which describes the *workaround* as if it were the rule:
   read literally it licenses dropping a requirement to escape a filter, which is a gate
   removal wearing a bug fix's clothes. One rule survives, and it is this one — the
   filter goes, the requirement stays.
   A filter on a gate that is NOT required is fine, and is a saved runner minute, under
   the tripwire convention already written into `pkg_orbit_inapp_purchases`: the moment
   that gate is added to `required_status_checks`, its filter is deleted in the same
   change. To cut cost on a gate that IS required, do it inside the job — cache, an
   early-exit step, a `concurrency:` group — so the check still reports a conclusion.
   Never at the `on:` trigger. A gate that can go silent is not a gate.
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
- **M34 auth-coverage gate is not a reusable workflow.** The script and its caller workflow are
  copied per repo, which contradicts the model every other gate here follows (predicate in
  `.github`, thin caller in the consumer). Two rollouts in, the copies are still byte-identical;
  the moment one diverges, the fleet has N detectors. Promote it to
  `reusable-route-auth-coverage.yml` before the remaining 13 rollouts, or accept the drift
  knowingly. Owner: machinery lane.
- **The pilot's caller workflow violates invariant 3.** `service_orbit_analytics#197` ships
  `on: pull_request: branches: [sandbox]`. Invariant 3 forbids exactly that on a check that is
  or may become required, and this file already lists `service_org_broadcast #150` among the
  costs. The first rollout (`service_org_broadcast#158`) therefore diverges deliberately with a
  bare `pull_request:`; the pilot should be corrected so the remaining rollouts inherit the
  right shape rather than the filtered one.

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
