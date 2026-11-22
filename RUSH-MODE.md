# Rush mode (RUSH-MODE-1)

**Why:** founder, 22 Nov 2026: during a rush of PRs, gates that historically catch nothing (especially slow ones) are switched off, then switched back on. It applies `NO-FEAR-CI-1`: CI waiting is downtime.

**The switch:** the org Actions variable **`CI_RUSH_SKIP`**, a comma-separated list of gate keys.
- A listed gate's job is skipped, and a skipped job reports success, so required checks don't block.
- Remove a key, or empty the variable, to switch that gate back on.
- No workflow edit is needed either way.

**Keys** (the reusable workflow's file name without `reusable-` and `.yml`):

`barrel-safety-gate`, `colors-safety-gate`, `consolidated-gates`, `dart-safety-gate`, `drizzle-journal-gate`, `flutter-analyze`, `gutter-gate`, `lockfile-pin-gate`, `rule84-flavor-fork-gate`, `sql-safety-gate`, `sql-typestring-safety-gate`, `taxo-contract-lint`, `taxo-lint`, `tsc-check`, `ux-wireframe-link`.

**Never in rush mode** (no hook on purpose):
- `declaration-gate`: data declarations and the analytics marks.
- `sql-execution-gate`: tenant scoping, a security gate.
- The critic, settle and notify workflows.

**Who sets it:** IAC (IAC-B01), from the measured catch rate of each gate (runner-minutes per real catch), and only gates with no real catches. The current list and the numbers behind it live in specs `infra-e2e/` (CI section).

**Example:** `CI_RUSH_SKIP=gutter-gate,ux-wireframe-link` skips those two everywhere; every other gate still runs.

`MVP_GATES_PAUSED=true` (the older switch) still works as before for the four gates that read it.
